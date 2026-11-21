"""GUET-AC 命令行。接口契约见 docs/interfaces.md 与 docs/protocol.md。"""

import argparse
import getpass
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import auth, config, qr, store
from .auth import GuetError
from .client import Client, active, kind
from .ledger import Ledger

CHANNELS = ("数字码", "GPS", "二维码")
STAGES = ("started", "action_required", "confirmed", "unconfirmed")
SUBMITTED = ("submitting", "confirmed", "unconfirmed")
MAX_MESSAGE = 200

JSON_ERRORS = (GuetError, OSError, ValueError, KeyError, TypeError, requests.RequestException)


def captcha_reader(image):
    with tempfile.TemporaryDirectory(prefix="guet-captcha-") as directory:
        target = Path(directory) / "captcha.jpg"
        target.write_bytes(image)
        if os.name == "nt":
            os.startfile(target)
        else:
            print(f"请打开验证码图片：{target}")
        return input("桂电图形验证码：")


def normalized(records):
    return [{**r, "id": str(r.get("rollcall_id", r.get("id")))} for r in records]


def report_activity(record, channel, stage, event_dir, message=None, test=False):
    """输出并写入一条事件；同一活动同一 stage 只写一次（test 事件除外）。"""
    title = " ".join(str(record.get("course_title") or "课程").split())[:120]
    defaults = {
        "started": f"{title}的{channel}签到已开始。",
        "action_required": f"{title}的{channel}签到需要手动处理。",
        "confirmed": f"{title}的{channel}签到已确认完成。",
        "unconfirmed": f"{title}的{channel}签到未确认，请查看畅课。",
    }
    text = (message or defaults.get(stage, defaults["action_required"]))
    text = " ".join(str(text).split())[:MAX_MESSAGE]
    directory = Path(event_dir) if event_dir else None
    destination = None
    if directory is not None:
        if test:
            seed = f"test:{uuid.uuid4().hex}:{stage}"
        else:
            seed = f"{record.get('id')}:{stage}"
        destination = directory / f"{hashlib.sha256(seed.encode()).hexdigest()}.json"
        if destination.exists():
            return destination
    if not test:
        print(text, flush=True)
    if directory is None:
        return None
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"stage": stage, "channel": channel, "message": text}
    if test:
        payload["test"] = True
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return destination
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def automatic_channels(cfg, *, auto=False, auto_number=False, auto_gps=False):
    """决定本次进程自动处理哪些通道。

    - `--auto`：严格按配置里的 auto_number/auto_gps/auto_qr；
    - `--auto-number`/`--auto-gps`：只强制开启对应通道（保持旧 --auto-number 语义）；
    - 默认（旧调用）：数字码与 GPS 自动处理，二维码由 auto_qr 控制。
    """
    enabled = set()
    if auto:
        if cfg["auto_number"]:
            enabled.add("数字码")
        if cfg["auto_gps"]:
            enabled.add("GPS")
        if cfg["auto_qr"]:
            enabled.add("二维码")
    if auto_number:
        enabled.add("数字码")
    if auto_gps:
        enabled.add("GPS")
    if not auto and not auto_number and not auto_gps:
        enabled.update(("数字码", "GPS"))
        if cfg["auto_qr"]:
            enabled.add("二维码")
    return enabled


def recover_pending(client, state, event_dir):
    """重启恢复：仍进行中的 submitting 不再提交，产生一次 unconfirmed。"""
    if not state.enabled:
        return
    pending = {key: entry for key, entry in state.load().items()
               if isinstance(entry, dict) and entry.get("stage") == "submitting"}
    if not pending:
        return
    current = {record["id"]: record for record in normalized(client.rollcalls()) if active(record)}
    for key, entry in pending.items():
        record = current.get(key)
        if record is not None:
            channel = entry.get("channel") or kind(record)
            title = " ".join(str(record.get("course_title") or "课程").split())[:120]
            report_activity(record, channel, "unconfirmed", event_dir,
                            message=f"{title}的{channel}签到上次提交结果不明，请登录畅课确认。")
        state.mark(key, "unconfirmed")


def config_stamp(path):
    """配置文件的 (mtime_ns, size)；不可读时返回 None。"""
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def refresh_config(cfg, config_path, stamp):
    """配置变化时重载；失败保留旧配置并只告警一行，返回 (cfg, stamp, changed)。"""
    if config_path is None:
        return cfg, stamp, False
    current = config_stamp(config_path)
    if current is None or (stamp is not None and current == stamp):
        return cfg, stamp, False
    try:
        cfg = config.load(config_path)
    except (GuetError, OSError, ValueError) as exc:
        print(f"警告：配置文件重载失败，继续使用旧配置：{exc}", file=sys.stderr, flush=True)
        return cfg, current, False
    return cfg, current, True


def watch(client, cfg, once=False, dry_run=False, auto_number=False, event_dir=None,
          auto_gps=False, auto=False, state_file=None, config_path=None):
    # 一个活动在任意重启前后最多发起一次写请求；dry-run 不写状态、不提交。
    enabled = automatic_channels(cfg, auto=auto, auto_number=auto_number, auto_gps=auto_gps)
    state = Ledger(state_file or (Path(event_dir) / "state.json" if event_dir else None))
    if not dry_run:
        recover_pending(client, state, event_dir)
    attempted = set()
    informed = set()
    announced = set()
    stamp = None
    while True:
        # 网页 config-update 写回后无需重启：每轮检查 mtime/size 并热加载。
        cfg, stamp, changed = refresh_config(cfg, config_path, stamp)
        if changed:
            enabled = automatic_channels(cfg, auto=auto, auto_number=auto_number,
                                         auto_gps=auto_gps)
        for record in normalized(client.rollcalls()):
            rid = record["id"]
            if not active(record):
                continue
            channel = kind(record)
            if channel not in CHANNELS:
                continue
            entry = state.get(rid)
            if entry and entry.get("stage") in SUBMITTED:
                continue
            label = f'{record.get("course_title", "课程")} / {rid} / {channel}'
            if rid not in announced:
                report_activity(record, channel, "started", event_dir)
                announced.add(rid)
            if channel not in enabled:
                continue
            if dry_run:
                if rid not in informed:
                    print(f"发现：{label}")
                    informed.add(rid)
                continue
            if rid in attempted:
                continue
            attempted.add(rid)
            course_id = record.get("course_id")
            if channel == "GPS":
                place = config.location_for(cfg, course_id)
                if place is None:
                    state.mark(rid, "action_required", course_id=course_id, channel=channel)
                    report_activity(record, channel, "action_required", event_dir,
                                    message=f"{label}：未绑定签到地点，无法自动提交 GPS 签到，"
                                            "请配置地点。")
                    continue
                options = {"place": place, "accuracy": float(cfg["accuracy"]),
                           "coordinate_system": cfg["submit_coordinate_system"]}
            elif channel == "数字码":
                try:
                    options = {"code": client.number_code(record)}
                except GuetError as exc:
                    state.mark(rid, "action_required", course_id=course_id, channel=channel)
                    report_activity(record, channel, "action_required", event_dir,
                                    message=f"{label}：读取数字码失败：{exc}")
                    continue
            else:
                try:
                    options = {"token": client.qr_token(record)}
                except GuetError as exc:
                    state.mark(rid, "action_required", course_id=course_id, channel=channel)
                    report_activity(record, channel, "action_required", event_dir,
                                    message=f"{label}：读取二维码失败：{exc}")
                    continue
            if not state.claim(rid, course_id=course_id, channel=channel):
                continue
            try:
                confirmed = client.submit(record, channel, **options)
            except GuetError:
                # 写请求可能已送达，按结果不明处理，绝不重复提交。
                state.mark(rid, "unconfirmed", course_id=course_id, channel=channel)
                report_activity(record, channel, "unconfirmed", event_dir,
                                message=f"{label}：提交结果不明，请登录畅课确认。")
                continue
            if confirmed:
                state.mark(rid, "confirmed", course_id=course_id, channel=channel)
                report_activity(record, channel, "confirmed", event_dir)
            else:
                state.mark(rid, "unconfirmed", course_id=course_id, channel=channel)
                report_activity(record, channel, "unconfirmed", event_dir)
        if once:
            return
        time.sleep(cfg["poll_seconds"])


def atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def backup_path(path):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.before-web-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.before-web-{stamp}-{counter}")
        counter += 1
    return candidate


def config_json(args):
    print(json.dumps(config.public(config.load(args.config)), ensure_ascii=False))


def config_check(args):
    config.load(args.config)


def config_update(args):
    path = Path(args.config)
    original = path.read_bytes()
    current = config.load(path)
    raw = sys.stdin.read(65537)
    if len(raw) > 65536:
        raise GuetError("配置更新内容过大。")
    try:
        updates = json.loads(raw)
    except ValueError as exc:
        raise GuetError("配置更新必须是 JSON 对象。") from exc
    if not isinstance(updates, dict):
        raise GuetError("配置更新必须是 JSON 对象。")
    unknown = sorted(set(updates) - set(config.FIELDS))
    if unknown:
        raise GuetError("不支持的配置项：" + "、".join(unknown))
    merged = config.validate({**current, **updates})
    atomic_write(backup_path(path), original)
    atomic_write(path, config.dumps(merged).encode("utf-8"))
    print(json.dumps(config.public(merged), ensure_ascii=False))


def status_json(args):
    session, device = store.load(args.account)
    try:
        client = Client(session, device)
        profile = client.profile()
        records = normalized(client.rollcalls())
        payload = {
            "profile": {"id": str(profile.get("id")), "name": str(profile.get("name", ""))},
            "rollcalls": [
                {
                    "id": record["id"],
                    "course_id": str(record.get("course_id")),
                    "course_title": record.get("course_title"),
                    "status": record.get("status"),
                    "kind": kind(record),
                    "active": bool(active(record)),
                }
                for record in records
            ],
            "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        print(json.dumps(payload, ensure_ascii=False))
    finally:
        session.close()


def qr_payload(raw):
    """把 stdin 的 JSON 载荷解析为 {courseId, rollcallId, data}。"""
    if len(raw) > 65536:
        raise GuetError("二维码载荷过大。")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise GuetError("二维码载荷不是 JSON。") from exc
    if not isinstance(data, dict):
        raise GuetError("二维码载荷必须是对象。")

    def checked(value, name):
        if isinstance(value, bool) or not str(value or "").isascii() \
                or not str(value or "").isdigit():
            raise GuetError(f"二维码缺少有效 {name}。")
        return str(value)

    if "text" in data:
        if not isinstance(data["text"], str):
            raise GuetError("text 必须是字符串。")
        payload = qr.parse(data["text"])
    else:
        payload = {
            "courseId": checked(data.get("courseId"), "courseId"),
            "rollcallId": checked(data.get("rollcallId"), "rollcallId"),
        }
        token = data.get("data")
        if not isinstance(token, str) or not token:
            raise GuetError("二维码缺少有效 data。")
        payload["data"] = token
    return payload


def qr_submit(args):
    def failed(stage, message):
        print(json.dumps({"ok": False, "stage": stage, "error": str(message)[:500]},
                         ensure_ascii=False), file=sys.stderr)
        return 2

    try:
        payload = qr_payload(sys.stdin.read(65537))
    except JSON_ERRORS as exc:
        return failed("parse", exc)
    try:
        session, device = store.load(args.account)
    except JSON_ERRORS as exc:
        return failed("session", exc)
    try:
        client = Client(session, device)
        record = {"id": payload["rollcallId"], "course_id": payload["courseId"]}
        try:
            confirmed = client.submit(record, "二维码", token=payload["data"])
        except GuetError as exc:
            return failed(getattr(exc, "stage", None) or "submit", exc)
        if confirmed:
            print(json.dumps({"ok": True, "confirmed": True, "message": "已确认签到。"},
                             ensure_ascii=False))
            return 0
        print(json.dumps({"ok": True, "confirmed": False,
                          "message": "已提交，请在畅课查看结果。"}, ensure_ascii=False))
        return 3
    finally:
        session.close()


def test_event(args):
    labels = {"started": "签到已开始", "action_required": "需要用户处理",
              "confirmed": "签到已确认完成", "unconfirmed": "签到结果未确认"}
    message = f"【测试】{args.channel}{labels[args.stage]}。"
    record = {"id": f"test-{uuid.uuid4().hex}", "course_title": "测试活动"}
    path = report_activity(record, args.channel, args.stage, args.event_dir,
                           message=message, test=True)
    print(path)
    return 0


def guarded(function, *args):
    """JSON 契约命令的统一错误出口：stderr 单行 {"error":...}，退出码 2。"""
    try:
        function(*args)
        return 0
    except JSON_ERRORS as exc:
        print(json.dumps({"error": str(exc)[:500]}, ensure_ascii=False), file=sys.stderr)
        return 2


def run(args):
    if args.command == "locations":
        if getattr(args, "json", False):
            places = list(config.locations().values())
            keys = ("id", "name", "latitude", "longitude", "source")
            payload = {"places": [
                {**{key: place[key] for key in keys},
                 **({"method": place["method"]} if place.get("method") else {})}
                for place in places
            ]}
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        for place in config.locations().values():
            print(f'{place["id"]}\t{place["name"]}\t'
                  f'{place["latitude"]:.7f}, {place["longitude"]:.7f} (WGS84)')
        return 0
    if args.command == "logout":
        store.remove(args.account)
        print("已移除本机登录会话。")
        return 0
    if args.command == "import-session":
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise GuetError("会话输入过大。")
        cookies = json.loads(raw)
        if not isinstance(cookies, list) or not 1 <= len(cookies) <= 128:
            raise GuetError("会话格式无效。")
        with requests.Session() as session:
            for row in cookies:
                if not isinstance(row, dict) or row.get("domain", "").lstrip(".") != "courses.guet.edu.cn":
                    raise GuetError("仅接受桂电畅课域名的会话。")
                expiration = row.get("expires", -1)
                session.cookies.set_cookie(requests.cookies.create_cookie(
                    name=row["name"], value=row["value"], domain=row["domain"],
                    path=row["path"], secure=True,
                    expires=int(expiration) if expiration > 0 else None,
                ))
            Client(session).profile()
            store.save(args.account, session)
        print("Pi 已验证并保存桂电畅课会话。")
        return 0
    if args.command == "login":
        username = input("桂电学号/工号：").strip()
        password = getpass.getpass("统一身份认证密码：")
        session = auth.login(username, password, captcha_reader)
        try:
            store.save(args.account, session)
            print("桂电畅课登录成功，会话已保存。")
        finally:
            session.close()
        return 0
    if args.command == "config-json":
        return guarded(config_json, args)
    if args.command == "config-check":
        return guarded(config_check, args)
    if args.command == "config-update":
        return guarded(config_update, args)
    if args.command == "test-event":
        return test_event(args)
    if args.command == "qr-submit":
        return qr_submit(args)
    if args.command == "status" and getattr(args, "json", False):
        return guarded(status_json, args)
    session, device = store.load(args.account)
    try:
        client = Client(session, device)
        profile = client.profile()
        if args.command == "status":
            print(f'已登录：{profile.get("name", profile["id"])}')
            for record in normalized(client.rollcalls()):
                print(json.dumps({key: record.get(key) for key in
                                  ("id", "course_id", "course_title", "status")},
                                 ensure_ascii=False), kind(record))
        elif args.command in ("watch", "once"):
            cfg = config.load(args.config)
            watch(client, cfg, once=args.command == "once", dry_run=args.dry_run,
                  auto_number=args.auto_number, event_dir=args.event_dir,
                  auto_gps=args.auto_gps, auto=args.auto, state_file=args.state_file,
                  config_path=args.config)
        elif args.command == "qr":
            # 内容从 stdin 输入，避免有效令牌留在 shell 历史中。
            payload = qr.parse(input("粘贴二维码原始内容："))
            record = {"id": payload["rollcallId"], "course_id": payload["courseId"]}
            confirmed = client.submit(record, "二维码", token=payload["data"])
            print("已确认签到" if confirmed else "已提交，请在畅课查看结果")
        elif args.command == "number":
            record = {"id": args.rollcall, "course_id": args.course}
            confirmed = client.submit(record, "数字码", code=input("课堂数字码：").strip())
            print("已确认签到" if confirmed else "已提交，请在畅课查看结果")
        store.save(args.account, session, device)
        return 0
    finally:
        session.close()


def build_parser():
    parser = argparse.ArgumentParser(description="桂电畅课助手 GUET-AC")
    parser.add_argument("--account", default="default", help="本机账号别名，各别名会话独立")
    parser.add_argument("--config", default="config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("login", "import-session", "logout", "qr"):
        commands.add_parser(command)
    locations = commands.add_parser("locations")
    locations.add_argument("--json", action="store_true", help="输出单行 JSON")
    status = commands.add_parser("status")
    status.add_argument("--json", action="store_true", help="输出单行 JSON（不含会话）")
    for command in ("watch", "once"):
        sub = commands.add_parser(command)
        sub.add_argument("--dry-run", action="store_true", help="只查询，不提交，也不写状态")
        sub.add_argument("--auto", action="store_true",
                         help="按配置的 auto_number/auto_gps/auto_qr 执行")
        sub.add_argument("--auto-number", action="store_true", help="强制自动处理数字码")
        sub.add_argument("--auto-gps", action="store_true", help="强制自动提交 GPS 签到")
        sub.add_argument("--event-dir", help="将事件与去重状态记录到本地目录")
        sub.add_argument("--state-file", help="覆盖去重状态文件路径")
    number = commands.add_parser("number")
    number.add_argument("course", help="畅课 course_id")
    number.add_argument("rollcall", help="签到活动编号")
    commands.add_parser("config-json")
    commands.add_parser("config-check")
    commands.add_parser("config-update")
    commands.add_parser("qr-submit")
    event = commands.add_parser("test-event")
    event.add_argument("--event-dir", required=True, help="事件目录")
    event.add_argument("--channel", choices=CHANNELS, default="数字码")
    event.add_argument("--stage", choices=STAGES, default="confirmed")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = run(args)
    except requests.RequestException:
        parser.exit(1, "错误：桂电认证请求失败，请检查网络。\n")
    except (GuetError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"错误：{exc}\n")
    except KeyboardInterrupt:
        print("\n已停止。")
        return
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
