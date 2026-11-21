import { execFile, spawn } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';

const exec = promisify(execFile);
const root = join(homedir(), '.local/share/guet-ac');
const python = join(root, '.venv/bin/python');
const guetAc = join(root, '.venv/bin/guet-ac');
const preview = join(root, 'guet_qr_preview.py');
const session = process.env.GUET_AC_SESSION_DIR || join(root, 'session');
const maxBytes = 8 * 1024 * 1024;
const maxOutput = 16 * 1024;

// One explicit stage per failure so the reply can say whether the image was
// unavailable, unreadable, not a Chaoxing QR code, or rejected by Chaoxing.
class StageError extends Error {
  constructor(stage, message) {
    super(message);
    this.stage = stage;
  }
}

const mediaHost = /(^|\.)qpic\.cn$|(^|\.)qq\.com$|(^|\.)qq\.com\.cn$/i;

async function downloadQqImage(url, limit) {
  let response;
  try {
    response = await fetch(url, { redirect: 'error', signal: AbortSignal.timeout(10000) });
  } catch {
    throw new StageError('download', 'QQ 图片下载失败，请重发原图或稍后再试喵。');
  }
  if (!response.ok || !response.body) {
    await response.body?.cancel();
    throw new StageError('download', 'QQ 图片下载失败，请重发原图或稍后再试喵。');
  }
  const declared = Number(response.headers.get('content-length'));
  if (declared > limit) {
    await response.body.cancel();
    throw new StageError('download', '图片超过 8 MiB，请压缩后再发喵。');
  }
  const chunks = [];
  let bytes = 0;
  try {
    for await (const chunk of response.body) {
      bytes += chunk.length;
      if (bytes > limit) throw new StageError('download', '图片超过 8 MiB，请压缩后再发喵。');
      chunks.push(chunk);
    }
  } catch (error) {
    if (error instanceof StageError) throw error;
    throw new StageError('download', 'QQ 图片下载中断，请重发原图喵。');
  }
  return Buffer.concat(chunks, bytes);
}

// Capture stdout of a bounded child process. Resolves with the exit code even
// on a non-zero exit; rejects only when the process cannot run or times out.
function capture(file, args, { input, timeout = 10000, env = process.env, maxBytes: limit = maxOutput } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(file, args, { stdio: ['pipe', 'pipe', 'pipe'], env });
    let stdout = '';
    let stderr = '';
    let bytes = 0;
    let settled = false;
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn(value);
    };
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      finish(reject, new Error('image processing timed out'));
    }, timeout);
    timer.unref?.();
    const collect = (target, chunk) => {
      bytes += chunk.length;
      if (bytes > limit) {
        child.kill('SIGKILL');
        finish(reject, new Error('image processing output exceeded limit'));
        return target;
      }
      return target + chunk;
    };
    child.stdout.on('data', chunk => { stdout = collect(stdout, chunk); });
    child.stderr.on('data', chunk => { stderr = collect(stderr, chunk); });
    child.once('error', error => finish(reject, error));
    child.once('close', code => finish(resolve, { code, stdout, stderr }));
    child.stdin.once('error', () => { /* EPIPE after an early exit is not a reply failure. */ });
    child.stdin.end(input ?? '');
  });
}

function childEnv() {
  const env = { ...process.env, GUET_AC_SESSION_DIR: session };
  try {
    const uid = process.getuid();
    const runtime = process.env.XDG_RUNTIME_DIR || `/run/user/${uid}`;
    env.XDG_RUNTIME_DIR = runtime;
    env.DBUS_SESSION_BUS_ADDRESS = process.env.DBUS_SESSION_BUS_ADDRESS || `unix:path=${runtime}/bus`;
    // Keep the OpenCV Aruco import outside the QQ bridge's 150 MiB cgroup.
    env.GUET_AC_ARUCO_SCOPE = '1';
  } catch { /* Non-Linux or no user manager: decode in-process. */ }
  return env;
}

function parseJson(stdout) {
  try {
    const value = JSON.parse(String(stdout).trim());
    return value && typeof value === 'object' ? value : null;
  } catch {
    return null;
  }
}

function decodeReply(result) {
  if (result?.ok !== true) {
    const stage = result?.stage;
    const error = result?.error;
    if (stage === 'image') {
      if (error === 'too_large') return '图片超过 8 MiB，请压缩后再发喵。';
      if (error === 'pixels_too_large') return '图片像素过大，请缩小后再发喵。';
      return '图片无法读取，请发原图喵。';
    }
    if (stage === 'parse' || error === 'invalid_payload') {
      return '读到了二维码，但不是畅课签到码喵。';
    }
    if (error === 'multiple_qr') return '图片里有多个二维码，请只拍一个签到码喵。';
    if (error === 'timeout') return '识别超时，请发更清晰的原图喵。';
    return '没能在图片里读出一个二维码喵。';
  }
  return null;
}

function submitReply(result) {
  if (result?.ok === true && result.confirmed === true) {
    return '已识别并提交，畅课已确认签到喵。';
  }
  if (result?.ok === true) {
    return '已识别并提交，畅课还没确认，请打开畅课查看喵。';
  }
  const stage = result?.stage;
  if (stage === 'session') return '二维码已识别，但畅课会话已失效，请更新会话后再发一次喵。';
  if (stage === 'parse') return '二维码内容不是有效的畅课签到码喵。';
  if (stage === 'network') return '二维码已识别，提交时网络中断，请在畅课确认是否已签到喵。';
  if (stage === 'submit') return '二维码有效，但提交被畅课拒绝（可能活动已结束）喵。';
  if (stage === 'verify') return '已提交，但回查结果异常，请打开畅课确认喵。';
  return '二维码已识别，但提交时出错，请打开畅课确认喵。';
}

export function createGuetPrivateRoute({ download = downloadQqImage, run = exec, runChild = capture } = {}) {
  return async ({ privateChat, bound = false, text = '', images = [], reply }) => {
    if (!privateChat) return false;
    const command = String(text).trim();
    if (command === '/畅课状态') {
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
    if (!images.length) return false;
    // Image recognition alone used to be harmless; submitting on behalf of the
    // account is not. Only a private chat already bound to a person may submit.
    if (!bound) {
      await reply('请先在猫猫私聊发送 /bind 完成绑定，再发签到码照片喵。');
      return true;
    }
    if (images.length !== 1 || !images[0].url) {
      await reply('请一次发送一张二维码图片喵。');
      return true;
    }
    let directory;
    let response;
    try {
      const url = new URL(images[0].url);
      if (url.protocol !== 'https:' || url.username || url.password || !mediaHost.test(url.hostname)) {
        throw new StageError('url', '这张图片的下载地址无效，请重新发送喵。');
      }
      const bytes = await (async () => {
        try {
          return await download(url.href, maxBytes);
        } catch (error) {
          throw new StageError('download',
            error?.stage === 'download' && error.message
              ? error.message : 'QQ 图片下载失败，请重发原图或稍后再试喵。');
        }
      })();
      if (!bytes.length) throw new StageError('download', 'QQ 图片为空，请重发原图喵。');
      directory = await mkdtemp(join(tmpdir(), 'emb-guet-'));
      const image = join(directory, 'upload');
      await writeFile(image, bytes, { mode: 0o600 });
      const decoded = await runChild(python, [preview, '--json', image], {
        timeout: 10000, env: childEnv(),
      });
      const result = parseJson(decoded.stdout) || parseJson(decoded.stderr);
      if (decoded.code !== 0 || result?.ok !== true) {
        response = decodeReply(result) || '没能在图片里读出一个二维码喵。';
      } else {
        const submitted = await runChild(guetAc, ['qr-submit'], {
          input: JSON.stringify(result.payload),
          timeout: 30000,
          env: childEnv(),
        });
        response = submitReply(parseJson(submitted.stdout) || parseJson(submitted.stderr));
      }
    } catch (error) {
      response = error instanceof StageError ? error.message : '这张图片处理失败，请重发原图喵。';
    } finally {
      if (directory) await rm(directory, { force: true, recursive: true });
    }
    await reply(response);
    return true;
  };
}
