import argparse
import getpass
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests

from . import auth, config, qr, store
from .auth import GuetError
from .client import Client, active, kind


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


def report_activity(record, channel, stage, event_dir):
    title = " ".join(str(record.get("course_title") or "课程").split())[:120]
    messages = {
        "started": f"{title}的{channel}签到已开始。",
        "confirmed": f"{title}的{channel}签到已确认完成。",
        "unconfirmed": f"{title}的{channel}签到未确认，请查看畅课。",
    }
    print(messages[stage], flush=True)
    if not event_dir:
        return
    directory = Path(event_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{record['id']}:{stage}".encode()).hexdigest()
    destination = directory / f"{digest}.json"
    if destination.exists():
        return
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"stage": stage, "channel": channel, "message": messages[stage]},
                      stream, ensure_ascii=False)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
    finally:
        Path(temporary).unlink(missing_ok=True)


def watch(client, cfg, once=False, dry_run=False, auto_number=False, event_dir=None):
    # 一个进程对一个活动最多提交一次；网络不确定时也不自动重复写请求。
    attempted = set()
    informed = set()
    announced = set()
    while True:
        for record in normalized(client.rollcalls()):
            rid = record["id"]
            if not active(record) or rid in attempted:
                continue
            channel = kind(record)
            label = f'{record.get("course_title", "课程")} / {rid} / {channel}'
            if rid not in announced and channel in {"数字码", "GPS", "二维码"}:
                report_activity(record, channel, "started", event_dir)
                announced.add(rid)
            if auto_number and channel != "数字码":
                continue
            if dry_run:
                if rid not in informed:
                    print(f"发现：{label}")
                    informed.add(rid)
                continue
            options = {}
            try:
                if channel == "GPS":
                    place = config.location_for(cfg, record.get("course_id"))
                    if place is None:
                        raise GuetError("请为课程配置地点或选择默认地点。")
                    options = {"place": place, "accuracy": cfg["accuracy"],
                               "coordinate_system": cfg["submit_coordinate_system"]}
                elif channel == "数字码":
                    options = {"code": client.number_code(record)}
                elif channel == "二维码" and cfg["auto_qr"]:
                    options = {"token": client.qr_token(record)}
                else:
                    raise GuetError("此类型仅提醒；二维码自动取码由 auto_qr 配置启用。")
                attempted.add(rid)
                confirmed = client.submit(record, channel, **options)
                report_activity(record, channel, "confirmed" if confirmed else "unconfirmed", event_dir)
            except GuetError as exc:
                if rid in attempted:
                    report_activity(record, channel, "unconfirmed", event_dir)
                if rid not in informed:
                    print(f"{label}：{exc}")
                    informed.add(rid)
                # 无权限/缺码仅提醒，避免对同一活动反复探测取码。
                attempted.add(rid)
        if once:
            return
        time.sleep(cfg["poll_seconds"])


def run(args):
    if args.command == "locations":
        for place in config.locations().values():
            print(f'{place["id"]}\t{place["name"]}\t'
                  f'{place["latitude"]:.7f}, {place["longitude"]:.7f} (WGS84)')
        return
    if args.command == "logout":
        store.remove(args.account)
        print("已移除本机登录会话。")
        return
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
        return
    if args.command == "login":
        username = input("桂电学号/工号：").strip()
        password = getpass.getpass("统一身份认证密码：")
        session = auth.login(username, password, captcha_reader)
        try:
            store.save(args.account, session)
            print("桂电畅课登录成功，会话已保存。")
        finally:
            session.close()
        return
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
            watch(client, cfg, args.command == "once", args.dry_run, args.auto_number, args.event_dir)
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
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description="桂电畅课助手 GUET-AC")
    parser.add_argument("--account", default="default", help="本机账号别名，各别名会话独立")
    parser.add_argument("--config", default="config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("login", "import-session", "logout", "status", "locations", "qr"):
        commands.add_parser(command)
    for command in ("watch", "once"):
        sub = commands.add_parser(command)
        mode = sub.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="只查询，不提交")
        mode.add_argument("--auto-number", action="store_true", help="自动处理数字码，GPS 和二维码仅提示")
        sub.add_argument("--event-dir", help="将活动开始和提交结果记录到本地目录")
    number = commands.add_parser("number")
    number.add_argument("course", help="畅课 course_id")
    number.add_argument("rollcall", help="签到活动编号")
    args = parser.parse_args()
    try:
        run(args)
    except requests.RequestException:
        parser.exit(1, "错误：桂电认证请求失败，请检查网络。\n")
    except (GuetError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"错误：{exc}\n")
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
