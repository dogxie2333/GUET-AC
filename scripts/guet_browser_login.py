"""User-operated GUET sign-in in a fresh Chrome window, then import over SSH."""

import argparse
import json
import subprocess
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

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
            print("请在新窗口完成桂电认证。返回畅课首页后，在这里按 Enter。")
            input()
            if urlsplit(page.url).hostname != "courses.guet.edu.cn":
                parser.exit(1, "当前页面尚未返回桂电畅课。\n")
            response = context.request.get(COURSES + "/api/profile", max_redirects=0)
            if not response.ok or not response.json().get("id"):
                parser.exit(1, "浏览器中未检测到有效的畅课会话。\n")
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
