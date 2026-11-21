"""活动状态账本：事件目录 state.json 的原子读写、去重与重启恢复。

契约见 docs/interfaces.md 第 1 节：
- 文件 0600、temp+replace 原子替换；损坏或异形文件按空账本处理；
- 写请求前先持久化 submitting，请求后写 confirmed/unconfirmed；
- 超过 14 天或超过 500 条的记录在写入时清理；
- 文件名固定为 state.json（不是 64 位十六进制），不会被通知扫描当作事件。
"""

import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_AGE = timedelta(days=14)
MAX_ENTRIES = 500
SUBMITTED_STAGES = ("submitting", "confirmed", "unconfirmed")
LOCK_TIMEOUT = 5.0


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def prune(activities):
    """丢弃超过 14 天的记录，并只保留最新的 500 条。"""
    moment = datetime.now(timezone.utc)
    kept = {}
    for key, entry in activities.items():
        if not isinstance(entry, dict):
            continue
        stamp = timestamp(entry.get("updated_at"))
        if stamp is not None and moment - stamp > MAX_AGE:
            continue
        kept[str(key)] = entry
    if len(kept) > MAX_ENTRIES:
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        ordered = sorted(
            kept.items(),
            key=lambda item: timestamp(item[1].get("updated_at")) or oldest,
            reverse=True,
        )
        kept = dict(ordered[:MAX_ENTRIES])
    return kept


class Ledger:
    """事件目录下的去重账本；path 为 None 时退化为纯内存（不持久化）。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else None

    @property
    def enabled(self):
        return self.path is not None

    def load(self):
        if not self.enabled:
            return {}
        return prune(self._read())

    def get(self, key):
        return self.load().get(str(key))

    def claim(self, key, *, course_id=None, channel=None):
        """在发起写请求前持久化 submitting；已有提交记录时返回 False。"""
        if not self.enabled:
            return True
        with self._lock():
            activities = prune(self._read())
            entry = activities.get(str(key))
            if entry and entry.get("stage") in SUBMITTED_STAGES:
                return False
            activities[str(key)] = {
                "course_id": None if course_id is None else str(course_id),
                "channel": channel,
                "stage": "submitting",
                "updated_at": now(),
            }
            self._write(prune(activities))
            return True

    def mark(self, key, stage, *, course_id=None, channel=None):
        """记录活动结果；mark 不会把已提交状态降级为可再次提交。"""
        if not self.enabled:
            return
        with self._lock():
            activities = prune(self._read())
            entry = dict(activities.get(str(key)) or {})
            entry["stage"] = stage
            entry["updated_at"] = now()
            if course_id is not None:
                entry["course_id"] = str(course_id)
            if channel is not None:
                entry["channel"] = channel
            activities[str(key)] = entry
            self._write(prune(activities))

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        activities = data.get("activities") if isinstance(data, dict) else None
        if not isinstance(activities, dict):
            return {}
        return dict(activities)

    def _write(self, activities):
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "activities": activities}, stream,
                          ensure_ascii=False, sort_keys=True)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _lock(self):
        """跨进程互斥，避免两个 watch 同时为同一活动发写请求。"""
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = directory / (self.path.name + ".lock")
        deadline = time.monotonic() + LOCK_TIMEOUT
        handle = None
        while handle is None:
            try:
                handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if time.monotonic() < deadline:
                    time.sleep(0.02)
                    continue
                # 超时视为陈旧锁：清理后重试一次，仍失败则放弃加锁继续。
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                try:
                    handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    break
        try:
            yield
        finally:
            if handle is not None:
                os.close(handle)
                lock_path.unlink(missing_ok=True)
