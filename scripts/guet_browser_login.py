"""User-operated GUET sign-in in a fresh Chrome window, then import over SSH."""

import argparse
import json
import subprocess
from time import monotonic
from urllib.parse import urlsplit

from playwright.sync_api import Error, TimeoutError, sync_playwright

COURSES = "https://courses.guet.edu.cn"
PI = "orangepi@172.16.8.147"
IMPORT = ('cd ~/.local/share/guet-ac && '
          'GUET_AC_SESSION_DIR="$PWD/session" .venv/bin/guet-ac import-session')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi", default=PI, help="SSH destination")
    args = parser.parse_args()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(COURSES + "/user/index", wait_until="domcontentloaded", timeout=60000)
            print("请在本命令打开的新 Chrome 窗口登录。终端会自动检测，不用按 Enter。", flush=True)
            deadline = monotonic() + 600
            last_hosts = ""
            profile = None
            while monotonic() < deadline and browser.is_connected():
                hosts = "、".join(sorted({
                    urlsplit(item.url).hostname for item in context.pages
                    if urlsplit(item.url).hostname in {
                        "courses.guet.edu.cn", "cas.guet.edu.cn", "identity.guet.edu.cn"
                    }
                }))
                if hosts and hosts != last_hosts:
                    print(f"当前窗口：{hosts}", flush=True)
                    last_hosts = hosts
                course_pages = [
                    item for item in context.pages
                    if urlsplit(item.url).hostname == "courses.guet.edu.cn"
                ]
                for item in reversed(course_pages):
                    try:
                        result = item.evaluate("""
                            async () => {
                                const response = await fetch('/api/profile', {
                                    credentials: 'same-origin',
                                    headers: {Accept: 'application/json'},
                                    signal: AbortSignal.timeout(5000)
                                });
                                const body = response.ok ? await response.json().catch(() => null) : null;
                                return {status: response.status, hasId: Boolean(body?.id)};
                            }
                        """)
                        if result != profile:
                            print(f"畅课个人资料接口 HTTP {result['status']}", flush=True)
                            profile = result
                        if profile["hasId"]:
                            break
                    except Error:
                        # The browser can be redirecting while the user finishes SSO.
                        continue
                if profile and profile["hasId"]:
                    break
                try:
                    context.wait_for_event("page", timeout=1500)
                except TimeoutError:
                    pass
            else:
                parser.exit(1, "等待结束，未在本次窗口检测到有效的畅课会话。\n")
            cookies = context.cookies()
            scoped = [
                {key: row[key] for key in ("name", "value", "domain", "path", "secure", "expires")}
                for row in cookies if row["domain"].lstrip(".") == "courses.guet.edu.cn"
            ]
            if not scoped:
                parser.exit(1, "浏览器没有返回畅课专用会话。\n")
            print(f"将仅把畅课会话经 SSH 交给 {args.pi}，不包含学校密码或 CAS 会话。")
            if input("确认请输入 yes：").strip().lower() != "yes":
                parser.exit(1, "已取消。\n")
            result = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "--", args.pi, IMPORT],
                input=json.dumps(scoped),
                text=True,
                check=False,
            )
            return result.returncode
        finally:
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
