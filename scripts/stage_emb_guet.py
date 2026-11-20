from pathlib import Path
import shutil
import subprocess

root = Path.home() / "emb-maibot-staging" / "emb-qq-official"
source = root / "main.js"
backup = root / "main.js.before-guet"
stage = root / "main.guet-stage.js"
data = source.read_bytes()
eol = b"\r\n" if b"\r\n" in data else b"\n"
anchor = b'import { randomBytes } from "node:crypto";'
route = (
    b'  async function onIncoming(peerKey, text, targets, images = [], replyMsgId = "", '
    b'background = "", ambient = false, speaker = null, groupHistory = "", '
    b'voiceReply = false, event = null) {'
)
if data.count(route) != 1 or data.count(anchor) != 1 or b"createGuetPrivateRoute" in data:
    raise SystemExit("QQ bridge source no longer matches expected entry point")
data = data.replace(anchor, anchor + eol + b'import { createGuetPrivateRoute } from "./emb-guet-private.mjs";')
hook = """  const guetPrivate = createGuetPrivateRoute();
__ROUTE__
    if (!ambient && event?.message_type === 'private') {
      const userId = String(event.user_id || '');
      if (userId && await guetPrivate({
        privateChat: true, bound: Boolean(bindings[userId]?.person), peer: userId,
        text: String(text || ''), images, reply: message => reply(targets, message),
      })) return;
    }"""
data = data.replace(route, hook.replace("__ROUTE__", route.decode()).encode().replace(b"\n", eol))
shutil.copyfile(Path.home() / ".local/share/guet-ac/emb_guet_private.mjs", root / "emb-guet-private.mjs")
stage.write_bytes(data)
node = Path.home() / ".local/node24/bin/node"
subprocess.run([str(node), "--check", str(stage)], check=True)
if not backup.exists():
    shutil.copy2(source, backup)
print(stage)
