"""Stage the GUET private route into the EMB QQ bridge source.

The bridge source is not kept in this repository. This script is intentionally
idempotent: when the hook is already present it only refreshes the private route
module and validates the source, so a second deployment cannot double-inject.

Usage (on the Pi):
    python3 stage_emb_guet.py
    python3 stage_emb_guet.py --source .../main.js --output .../main.guet-stage.js
"""

import argparse
import shutil
import subprocess
from pathlib import Path

HOME = Path.home()
DEFAULT_ROOT = HOME / "emb-maibot-staging" / "emb-qq-official"
DEFAULT_APP = HOME / ".local" / "share" / "guet-ac"
MARKER = b"createGuetPrivateRoute"
ANCHOR = b'import { randomBytes } from "node:crypto";'
ROUTE = (b'  async function onIncoming(peerKey, text, targets, images = [], replyMsgId = "", '
         b'background = "", ambient = false, speaker = null, groupHistory = "", '
         b'voiceReply = false, event = null) {')
IMPORT = b'import { createGuetPrivateRoute } from "./emb-guet-private.mjs";'
INIT = b'  const guetPrivate = createGuetPrivateRoute();'
HOOK = b"""  async function onIncoming(peerKey, text, targets, images = [], replyMsgId = "", background = "", ambient = false, speaker = null, groupHistory = "", voiceReply = false, event = null) {
    if (!ambient && event?.message_type === 'private') {
      const userId = String(event.user_id || '');
      if (userId && await guetPrivate({
        privateChat: true, bound: Boolean(bindings[userId]?.person), peer: userId,
        text: String(text || ''), images, reply: message => reply(targets, message),
      })) return;
    }"""


def node_check(path):
    node = HOME / ".local" / "node24" / "bin" / "node"
    if not node.is_file():
        raise SystemExit(f"未找到 Node：{node}")
    subprocess.run([str(node), "--check", str(path)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_ROOT / "main.js")
    parser.add_argument("--output", type=Path, default=DEFAULT_ROOT / "main.guet-stage.js")
    parser.add_argument("--private-route", type=Path,
                        default=DEFAULT_APP / "emb_guet_private.mjs")
    parser.add_argument("--no-private-route", action="store_true",
                        help="只生成桥接源码，不复制私聊路由模块")
    args = parser.parse_args()

    source = args.source
    data = source.read_bytes()
    if not args.no_private_route:
        if not args.private_route.is_file():
            raise SystemExit(f"缺少私聊路由模块：{args.private_route}")
        shutil.copyfile(args.private_route, DEFAULT_ROOT / "emb-guet-private.mjs")

    if MARKER in data:
        # Already injected by an earlier deployment. Never rewrite the bridge
        # main.js; only confirm that the hook shape and private route stay valid.
        for required in (IMPORT, INIT, b"bound: Boolean(bindings[userId]?.person)",
                         b"images, reply: message => reply(targets, message)"):
            if required not in data:
                raise SystemExit("桥接源码已注入，但 hook 结构不完整，停止部署。")
        node_check(source)
        if not args.no_private_route:
            node_check(DEFAULT_ROOT / "emb-guet-private.mjs")
        print(source)
        print("already-staged")
        return 0

    if data.count(ANCHOR) != 1 or data.count(ROUTE) != 1:
        raise SystemExit("QQ bridge source no longer matches expected entry point")
    original = data
    backup = DEFAULT_ROOT / "main.js.before-guet"
    if not backup.exists():
        backup.write_bytes(original)
        backup.chmod(0o600)
    eol = b"\r\n" if b"\r\n" in data else b"\n"
    data = data.replace(ANCHOR, ANCHOR + eol + IMPORT)
    data = data.replace(ROUTE, HOOK.replace(b"\n", eol))
    args.output.write_bytes(data)
    node_check(args.output)
    node_check(DEFAULT_ROOT / "emb-guet-private.mjs")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
