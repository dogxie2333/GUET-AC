"""验证 scripts/stage_web_guet.py 的注入、幂等与失败保护。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "stage_web_guet.py"

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

FIXTURE = f"""{IMPORT_ANCHOR}
import {{ sameOrigin }} from "./resource-access.mjs";

let qqSettingsHandler = null;
{INIT_ANCHOR}

export async function route(req, res, {{ pathname, person, isAdmin }}) {{
  {CALL_ANCHOR}
  {NEXT_ANCHOR}isAdmin ? "admin" : "web-ai";
  return presetFor;
}}
"""


def write_file(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="")
    return path


def run_stage(*args: object) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *[str(arg) for arg in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=env,
    )


def stage_once(tmp_path: Path, fixture: str = FIXTURE) -> tuple[Path, Path, subprocess.CompletedProcess[str]]:
    source = write_file(tmp_path / "server.mjs", fixture)
    output = tmp_path / "out.mjs"
    completed = run_stage("--source", source, "--output", output)
    return source, output, completed


def test_injects_import_init_and_route_call(tmp_path: Path) -> None:
    _, output, completed = stage_once(tmp_path)
    assert completed.returncode == 0, completed.stderr
    content = output.read_text(encoding="utf-8")
    assert content.count(IMPORT_LINE) == 1
    assert content.count(INIT_LINE) == 1
    assert content.count(CALL_LINE) == 1
    assert IMPORT_ANCHOR + "\n" + IMPORT_LINE in content
    assert INIT_ANCHOR + "\n" + INIT_LINE in content
    assert CALL_ANCHOR + "\n" + CALL_LINE in content


def test_call_is_between_qq_settings_and_preset_for(tmp_path: Path) -> None:
    _, output, completed = stage_once(tmp_path)
    assert completed.returncode == 0, completed.stderr
    content = output.read_text(encoding="utf-8")
    assert content.index(INIT_ANCHOR) < content.index(INIT_LINE)
    assert content.index(CALL_ANCHOR) < content.index(CALL_LINE)
    assert content.index(CALL_LINE) < content.index(NEXT_ANCHOR)


def test_second_run_is_idempotent(tmp_path: Path) -> None:
    _, first, completed = stage_once(tmp_path)
    assert completed.returncode == 0, completed.stderr
    second = tmp_path / "out2.mjs"
    again = run_stage("--source", first, "--output", second)
    assert again.returncode == 0, again.stderr
    assert second.read_bytes() == first.read_bytes()


def test_in_place_run_is_idempotent(tmp_path: Path) -> None:
    _, output, completed = stage_once(tmp_path)
    assert completed.returncode == 0, completed.stderr
    before = output.read_bytes()
    again = run_stage("--source", output)
    assert again.returncode == 0, again.stderr
    assert output.read_bytes() == before


def test_missing_anchor_fails_without_touching_target(tmp_path: Path) -> None:
    fixture = FIXTURE.replace(INIT_ANCHOR + "\n", "")
    source = write_file(tmp_path / "server.mjs", fixture)
    output = write_file(tmp_path / "out.mjs", "// untouched\n")
    completed = run_stage("--source", source, "--output", output)
    assert completed.returncode != 0
    assert "锚点" in (completed.stderr + completed.stdout)
    assert output.read_text(encoding="utf-8") == "// untouched\n"
    assert source.read_text(encoding="utf-8") == fixture


def test_duplicate_anchor_fails_without_creating_target(tmp_path: Path) -> None:
    fixture = FIXTURE.replace(CALL_ANCHOR, CALL_ANCHOR + "\n  " + CALL_ANCHOR)
    source = write_file(tmp_path / "server.mjs", fixture)
    output = tmp_path / "out.mjs"
    completed = run_stage("--source", source, "--output", output)
    assert completed.returncode != 0
    assert not output.exists()


def test_partially_injected_file_fails(tmp_path: Path) -> None:
    fixture = FIXTURE.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + "\n" + IMPORT_LINE)
    source = write_file(tmp_path / "server.mjs", fixture)
    output = tmp_path / "out.mjs"
    completed = run_stage("--source", source, "--output", output)
    assert completed.returncode != 0
    assert not output.exists()
    assert source.read_text(encoding="utf-8") == fixture


def test_preserves_crlf_line_endings(tmp_path: Path) -> None:
    fixture = FIXTURE.replace("\n", "\r\n")
    source = write_file(tmp_path / "server.mjs", fixture)
    output = tmp_path / "out.mjs"
    completed = run_stage("--source", source, "--output", output)
    assert completed.returncode == 0, completed.stderr
    content = output.read_text(encoding="utf-8", newline="")
    assert "\r\n" in content
    assert "\n" not in content.replace("\r\n", "")
    assert IMPORT_ANCHOR + "\r\n" + IMPORT_LINE in content


def test_creates_backup_of_existing_target(tmp_path: Path) -> None:
    source = write_file(tmp_path / "server.mjs", FIXTURE)
    output = write_file(tmp_path / "out.mjs", "// previous deployment\n")
    completed = run_stage("--source", source, "--output", output)
    assert completed.returncode == 0, completed.stderr
    backup = tmp_path / "out.mjs.before-web-guet"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "// previous deployment\n"


def test_missing_source_fails(tmp_path: Path) -> None:
    completed = run_stage("--source", tmp_path / "absent.mjs", "--output", tmp_path / "out.mjs")
    assert completed.returncode != 0


def test_node_syntax_check_when_available(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("本机没有 node，跳过 node --check")
    _, output, completed = stage_once(tmp_path)
    assert completed.returncode == 0, completed.stderr
    check = subprocess.run(
        [shutil.which("node"), "--check", str(output)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert check.returncode == 0, check.stderr
