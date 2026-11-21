import assert from 'node:assert/strict';
import { mkdtemp, rm, stat, utimes, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { deliverGuetEvents } from '../../scripts/emb_guet_notify.mjs';

const nameFor = seed => createHash('sha256').update(seed).digest('hex') + '.json';

async function workspace(t) {
  const directory = await mkdtemp(join(tmpdir(), 'guet-notify-test-'));
  t.after(() => rm(directory, { force: true, recursive: true }));
  return directory;
}

async function event(directory, seed, payload, ageMs = 0) {
  const path = join(directory, nameFor(seed));
  await writeFile(path, JSON.stringify(payload), { mode: 0o600 });
  if (ageMs) {
    const at = new Date(Date.now() - ageMs);
    await utimes(path, at, at);
  }
  return path;
}

const binding = { qbot_c2c_USER: { person: 'owner' } };
const allowed = () => ({ platformAllowed: true, subscribed: true });

test('requires exactly one bound private chat', async t => {
  const directory = await workspace(t);
  await event(directory, 'one', { stage: 'confirmed', channel: '数字码', message: '完成' });
  assert.equal(await deliverGuetEvents({ directory, bindings: async () => ({}), permission: allowed, send: async () => {} }), 0);
  const two = { ...binding, qbot_c2c_OTHER: { person: 'owner' } };
  assert.equal(await deliverGuetEvents({ directory, bindings: async () => two, permission: allowed, send: async () => {} }), 0);
});

test('requires platform permission and explicit subscription', async t => {
  const directory = await workspace(t);
  await event(directory, 'perm', { stage: 'confirmed', channel: 'GPS', message: '完成' });
  let sent = 0;
  const send = async () => { sent++; };
  assert.equal(await deliverGuetEvents({ directory, bindings: async () => binding,
    permission: () => ({ platformAllowed: false, subscribed: true }), send }), 0);
  assert.equal(await deliverGuetEvents({ directory, bindings: async () => binding,
    permission: () => ({ platformAllowed: true, subscribed: false }), send }), 0);
  assert.equal(sent, 0);
});

test('sends a valid action_required event and claims it once', async t => {
  const directory = await workspace(t);
  const path = await event(directory, 'action', { stage: 'action_required', channel: 'GPS',
    message: 'GPS 签到已开始，但未配置地点，请打开畅课处理。' });
  const sent = [];
  const send = async (id, text) => { sent.push([id, text]); };
  assert.equal(await deliverGuetEvents({ directory, bindings: async () => binding, permission: allowed, send }), 1);
  assert.deepEqual(sent, [['qbot_c2c_USER', 'GPS 签到已开始，但未配置地点，请打开畅课处理。']]);
  assert.equal((await stat(path + '.dispatch')).isFile(), true);
  assert.equal(await deliverGuetEvents({ directory, bindings: async () => binding, permission: allowed, send }), 0);
  assert.equal(sent.length, 1);
});

test('delivers a marked test event through the same permission path', async t => {
  const directory = await workspace(t);
  await event(directory, 'test', { stage: 'confirmed', channel: '数字码', message: '【测试】数字码通知链路验证', test: true });
  const sent = [];
  const count = await deliverGuetEvents({ directory, bindings: async () => binding, permission: allowed,
    send: async (id, text) => { sent.push(text); } });
  assert.equal(count, 1);
  assert.match(sent[0], /【测试】/);
});

test('ignores stale, future and invalid events; stops on unknown send result', async t => {
  const directory = await workspace(t);
  await event(directory, 'stale', { stage: 'confirmed', channel: '数字码', message: '旧事件' }, 16 * 60_000);
  await event(directory, 'future', { stage: 'confirmed', channel: '数字码', message: '未来事件' }, -10_000);
  await event(directory, 'bad-stage', { stage: 'unknown', channel: '数字码', message: '坏事件' });
  await event(directory, 'bad-channel', { stage: 'confirmed', channel: '其他', message: '坏通道' });
  await event(directory, 'too-long', { stage: 'confirmed', channel: '数字码', message: 'x'.repeat(201) });
  assert.equal(await deliverGuetEvents({ directory, bindings: async () => binding, permission: allowed, send: async () => {} }), 0);

  await event(directory, 'sent', { stage: 'confirmed', channel: '数字码', message: '正常事件' });
  let calls = 0;
  const send = async () => { calls++; throw new Error('network lost'); };
  assert.equal(await deliverGuetEvents({ directory, bindings: async () => binding, permission: allowed, send }), 0);
  assert.equal(calls, 1);
  const claimed = await stat(join(directory, nameFor('sent') + '.dispatch'));
  assert.equal(claimed.isFile(), true);
});
