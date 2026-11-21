import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createGuetPrivateRoute } from '../../scripts/emb_guet_private.mjs';

const payload = { courseId: '11', rollcallId: '22', data: 'token' };
const image = [{ url: 'https://gchat.qpic.cn/download?fileid=test' }];

function routeWith({ decode, submit, download, run } = {}) {
  const calls = { download: 0, preview: 0, submit: 0, replies: [] };
  const route = createGuetPrivateRoute({
    download: download || (async () => { calls.download++; return Buffer.from('fake-image'); }),
    run: run || (async () => ({ stdout: '' })),
    runChild: async (file, args, options) => {
      if (args.includes('--json')) {
        calls.preview++;
        return decode || { code: 0, stdout: JSON.stringify({ ok: true, decoder: 'zxing', payload }) };
      }
      calls.submit++;
      assert.equal(options.input, JSON.stringify(payload));
      return submit || { code: 0, stdout: JSON.stringify({ ok: true, confirmed: true }) };
    },
  });
  const reply = async text => { calls.replies.push(text); };
  return { route, calls, reply };
}

test('bound private chat submits once and reports confirmed', async () => {
  const { route, calls, reply } = routeWith();
  assert.equal(await route({ privateChat: true, bound: true, text: '', images: image, reply }), true);
  assert.equal(calls.download, 1);
  assert.equal(calls.preview, 1);
  assert.equal(calls.submit, 1);
  assert.deepEqual(calls.replies, ['已识别并提交，畅课已确认签到喵。']);
});

test('unbound chat cannot submit on the account owner behalf', async () => {
  const { route, calls, reply } = routeWith();
  assert.equal(await route({ privateChat: true, bound: false, text: '', images: image, reply }), true);
  assert.equal(calls.download, 0);
  assert.match(calls.replies[0], /\/bind/);
});

test('distinguishes download, unreadable, decode, parse and submit failures', async () => {
  const cases = [
    [{ download: async () => { throw new Error('QQ image unavailable'); } },
      'QQ 图片下载失败，请重发原图或稍后再试喵。'],
    [{ decode: { code: 2, stdout: JSON.stringify({ ok: false, stage: 'image', error: 'unreadable', message: 'x' }) } },
      '图片无法读取，请发原图喵。'],
    [{ decode: { code: 2, stdout: JSON.stringify({ ok: false, stage: 'decode', error: 'no_qr', message: 'x' }) } },
      '没能在图片里读出一个二维码喵。'],
    [{ decode: { code: 2, stdout: JSON.stringify({ ok: false, stage: 'parse', error: 'invalid_payload', message: 'x' }) } },
      '读到了二维码，但不是畅课签到码喵。'],
    [{ submit: { code: 2, stdout: '', stderr: JSON.stringify({ ok: false, stage: 'session', error: '会话已过期' }) } },
      '二维码已识别，但畅课会话已失效，请更新会话后再发一次喵。'],
    [{ decode: { code: 2, stdout: '', stderr: JSON.stringify({ ok: false, stage: 'decode', error: 'no_qr', message: 'x' }) } },
      '没能在图片里读出一个二维码喵。'],
    [{ submit: { code: 3, stdout: JSON.stringify({ ok: true, confirmed: false }) } },
      '已识别并提交，畅课还没确认，请打开畅课查看喵。'],
  ];
  for (const [options, expected] of cases) {
    const { route, calls, reply } = routeWith(options);
    await route({ privateChat: true, bound: true, text: '', images: image, reply });
    assert.deepEqual(calls.replies, [expected], JSON.stringify(options));
  }
});

test('rejects non-Tencent image hosts without downloading', async () => {
  const { route, calls, reply } = routeWith();
  await route({ privateChat: true, bound: true, text: '',
    images: [{ url: 'https://evil.example/x.png' }], reply });
  assert.equal(calls.download, 0);
  assert.deepEqual(calls.replies, ['这张图片的下载地址无效，请重新发送喵。']);
});

test('non-private messages and text fall through', async () => {
  const { route, reply } = routeWith();
  assert.equal(await route({ privateChat: false, text: '', images: image, reply }), false);
  assert.equal(await route({ privateChat: true, text: '你好', images: [], reply }), false);
});
