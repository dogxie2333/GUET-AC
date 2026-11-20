import { execFile } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';

const exec = promisify(execFile);
const root = join(homedir(), '.local/share/guet-ac');
const python = join(root, '.venv/bin/python');
const maxBytes = 8 * 1024 * 1024;

async function downloadQqImage(url, limit) {
  const response = await fetch(url, { redirect: 'error', signal: AbortSignal.timeout(10000) });
  if (!response.ok || !response.body || Number(response.headers.get('content-length')) > limit) {
    await response.body?.cancel();
    throw new Error('QQ image unavailable');
  }
  const chunks = [];
  let bytes = 0;
  for await (const chunk of response.body) {
    bytes += chunk.length;
    if (bytes > limit) throw new Error('QQ image exceeds limit');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, bytes);
}

export function createGuetPrivateRoute({ download = downloadQqImage, run = exec, clock = Date.now } = {}) {
  const pending = new Map();
  return async ({ privateChat, bound, peer, text, images, reply }) => {
    if (!privateChat) return false;
    const command = text.trim();
    const qr = command === '/扫课';
    const status = command === '/畅课状态';
    if (!bound) {
      pending.delete(peer);
      if (!qr && !status) return false;
      await reply('请先在猫猫私聊发送 /bind 绑定 EMB 账号。');
      return true;
    }
    if (status) {
      let active = false;
      try {
        const runtimeDir = process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid()}`;
        const env = {
          ...process.env,
          XDG_RUNTIME_DIR: runtimeDir,
          DBUS_SESSION_BUS_ADDRESS: process.env.DBUS_SESSION_BUS_ADDRESS || `unix:path=${runtimeDir}/bus`,
        };
        await run('systemctl', ['--user', 'is-active', '--quiet', 'guet-ac'], { timeout: 4000, env });
        active = true;
      } catch { /* Service is not active or the user bus is unavailable. */ }
      await reply(active ? '畅课只读监控正在运行喵。' : '畅课监控尚未运行喵。');
      return true;
    }
    const now = clock();
    for (const [id, end] of pending) if (end <= now) pending.delete(id);
    if (!qr && !(images.length && pending.has(peer))) return false;
    if (!images.length) {
      if (pending.size >= 64 && !pending.has(peer)) return false;
      pending.set(peer, now + 90_000);
      await reply('请在 90 秒内发一张二维码图片。我会在 Pi 上识别并回复结果喵。');
      return true;
    }
    pending.delete(peer);
    if (images.length !== 1 || !images[0].url) {
      await reply('请一次发送一张二维码图片喵。');
      return true;
    }
    let directory;
    let response;
    try {
      const url = new URL(images[0].url);
      if (url.protocol !== 'https:' || url.username || url.password ||
          !(/(^|\.)qpic\.cn$|(^|\.)qq\.com$/i.test(url.hostname))) {
        throw new Error('Unexpected QQ media URL');
      }
      const bytes = await download(url.href, maxBytes);
      if (!bytes.length || bytes.length > maxBytes) throw new Error('Image exceeds limit');
      directory = await mkdtemp(join(tmpdir(), 'emb-guet-'));
      const image = join(directory, 'upload');
      await writeFile(image, bytes, { mode: 0o600 });
      await run(python, [join(root, 'guet_qr_preview.py'), image], {
        timeout: 5000, maxBuffer: 4096,
      });
      response = '已在 Pi 上识别出一个二维码喵。';
    } catch {
      response = '没能识别二维码，请发清晰的单张图片喵。';
    } finally {
      if (directory) await rm(directory, { force: true, recursive: true });
    }
    await reply(response);
    return true;
  };
}
