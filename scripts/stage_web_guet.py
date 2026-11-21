#!/usr/bin/env python3
"""把 GUET-AC 课程地点配置 API 注入 emb-web-ai 的 server.mjs。

默认输入/输出均为 /home/orangepi/emb-web-ai/server.mjs。注入是幂等的：已注入的文件
原样输出、不再改动。锚点缺失/不唯一、或写出前 node --check 失败时非零退出且不写目标。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_SERVER = Path("/home/orangepi/emb-web-ai/server.mjs")

IMPORT_ANCHOR = 'import { createQQReminders } from "./qq-reminders.mjs";'
IMPORT_LINE = 'import { createGuetConfig } from "./guet-config.mjs";'
INIT_ANCHOR = "let qqSettingsSameOrigin = sameOrigin;"
INIT_LINE = "let guetConfigHandler = createGuetConfig({});"
CALL_ANCHOR = (
    "if (await qqSettingsHandler(req,res,"
    "{pathname,person,isAdmin,sameOrigin:qqSettingsSameOrigin})) return;"
)
CALL_LINE = "  if (await guetConfigHandler(req,res,{pathname,person,isAdmin,sameOrigin})) return;"
NEXT_ANCHOR = "const presetFor = "

REQUIRED_LINES = (IMPORT_LINE, INIT_LINE, CALL_LINE)


class InjectError(Exception):
    """锚点或已有注入状态不符合预期。"""


def _count_unique(text: str, anchor: str, label: str) -> int:
    occurrences = text.count(anchor)
    if occurrences != 1:
        raise InjectError(f"锚点不唯一或缺失：{label}（出现 {occurrences} 次）")
    return occurrences


def inject(text: str) -> tuple[str, bool]:
    """返回 (注入后文本, 是否有改动)。失败抛 InjectError。"""
    found = [line for line in REQUIRED_LINES if line in text]
    if len(found) == len(REQUIRED_LINES):
        return text, False
    if found:
        raise InjectError(
            "目标文件已包含部分 GUET 注入（createGuetConfig 存在但不完整），请先人工恢复后再试。"
        )

    for anchor, label in (
        (IMPORT_ANCHOR, "createQQReminders import"),
        (INIT_ANCHOR, "qqSettingsSameOrigin 初始化"),
        (CALL_ANCHOR, "qqSettingsHandler 调用"),
        (NEXT_ANCHOR, "presetFor 行"),
    ):
        _count_unique(text, anchor, label)

    init_at = text.find(INIT_ANCHOR)
    call_at = text.find(CALL_ANCHOR)
    preset_at = text.find(NEXT_ANCHOR)
    if not init_at < call_at < preset_at:
        raise InjectError("目标文件结构不符合预期：初始化/路由调用/presetFor 顺序异常。")

    eol = "\r\n" if "\r\n" in text else "\n"
    updated = text.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + eol + IMPORT_LINE, 1)
    updated = updated.replace(INIT_ANCHOR, INIT_ANCHOR + eol + INIT_LINE, 1)
    updated = updated.replace(CALL_ANCHOR, CALL_ANCHOR + eol + CALL_LINE, 1)
    return updated, True


def _resolve_node(explicit: str | None) -> str | None:
    if explicit:
        found = shutil.which(explicit)
        if found:
            return found
        raise InjectError(f"指定的 node 不可执行：{explicit}")
    return shutil.which("node")


def _read_preserving_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_preserving_newlines(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _check_syntax(node: str, content: str, suffix: str) -> None:
    """把内容写进临时文件后跑 node --check，失败抛 InjectError。"""
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=suffix or ".mjs", newline="", delete=False
        ) as handle:
            handle.write(content)
            temp_path = handle.name
        completed = subprocess.run(
            [node, "--check", temp_path], capture_output=True, text=True, timeout=120
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            reason = detail[-1] if detail else f"退出码 {completed.returncode}"
            raise InjectError(f"node --check 失败：{reason}")
    except FileNotFoundError as exc:  # pragma: no cover - 取决于环境
        raise InjectError(f"无法运行 node 语法检查：{exc}") from exc
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def stage(source: Path, output: Path, *, check: bool = True, node: str | None = None) -> int:
    try:
        original = _read_preserving_newlines(source)
    except OSError as exc:
        print(f"读取源文件失败：{exc}", file=sys.stderr)
        return 1

    try:
        updated, changed = inject(original)
    except InjectError as exc:
        print(f"注入失败，未写入目标：{exc}", file=sys.stderr)
        return 1

    if not changed:
        # 已注入：原样输出。就地运行时不动文件，避免无谓改写。
        try:
            if output.resolve() != source.resolve():
                output.parent.mkdir(parents=True, exist_ok=True)
                _write_preserving_newlines(output, original)
            print(f"已注入，未改动：{output}")
        except OSError as exc:
            print(f"写入目标失败：{exc}", file=sys.stderr)
            return 1
        return 0

    if check:
        try:
            node_path = _resolve_node(node)
            if node_path is None:
                print("未找到 node，跳过语法检查。", file=sys.stderr)
            else:
                _check_syntax(node_path, updated, output.suffix)
        except InjectError as exc:
            print(f"注入失败，未写入目标：{exc}", file=sys.stderr)
            return 1

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        backup = output.with_name(output.name + ".before-web-guet")
        if output.exists() and not backup.exists():
            shutil.copy2(output, backup)
        temp = output.with_name(output.name + ".guet-stage.tmp")
        _write_preserving_newlines(temp, updated)
        os.replace(temp, output)
    except OSError as exc:
        print(f"写入目标失败：{exc}", file=sys.stderr)
        return 1
    print(f"已注入：{output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="向 emb-web-ai server.mjs 幂等注入 GUET-AC 配置路由。")
    parser.add_argument("--source", type=Path, default=DEFAULT_SERVER, help="待注入的 server.mjs")
    parser.add_argument("--output", type=Path, default=None, help="输出路径；默认覆盖 --source")
    parser.add_argument("--no-check", dest="check", action="store_false", help="跳过 node --check")
    parser.add_argument("--check", dest="check", action="store_true", help="写出前执行 node --check（默认）")
    parser.add_argument("--node", default=None, help="node 可执行文件路径；默认从 PATH 查找")
    parser.set_defaults(check=True)
    args = parser.parse_args(argv)
    output = args.output or args.source
    return stage(args.source, output, check=args.check, node=args.node)


if __name__ == "__main__":
    raise SystemExit(main())
