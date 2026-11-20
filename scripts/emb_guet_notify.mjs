import { open, readFile, readdir, stat } from 'node:fs/promises';
import { join } from 'node:path';

const eventName = /^[a-f0-9]{64}\.json$/;
const knownChannels = new Set(['数字码', 'GPS', '二维码']);
const knownStages = new Set(['started', 'confirmed', 'unconfirmed']);

export async function deliverGuetEvents({ directory, bindings, permission, send, now = Date.now }) {
  const all = await bindings();
  const privateBindings = Object.entries(all).filter(([id, value]) =>
    /^qbot_c2c_[a-zA-Z0-9_-]+$/.test(id) && typeof value?.person === 'string' && value.person);
  if (privateBindings.length !== 1) return 0;
  const [[id, {person}]] = privateBindings;
  const allowed = () => {
    const grant = permission(person, 'private', id);
    return grant.platformAllowed && grant.subscribed;
  };
  if (!allowed()) return 0;
  let names;
  try { names = await readdir(directory); }
  catch (error) { if (error.code === 'ENOENT') return 0; throw error; }
  let sent = 0;
  const candidates = [];
  for (const name of names.filter(name => eventName.test(name))) {
    const path = join(directory, name);
    const info = await stat(path);
    const age = now() - info.mtimeMs;
    if (info.isFile() && info.size <= 4096 && age >= -5000 && age <= 15 * 60_000) {
      candidates.push({path, at:info.mtimeMs});
    }
  }
  for (const {path} of candidates.sort((a,b) => a.at-b.at)) {
    if (sent >= 4) break;
    let event;
    try { event = JSON.parse(await readFile(path, 'utf8')); } catch { continue; }
    if (!knownStages.has(event?.stage) || !knownChannels.has(event?.channel) ||
        typeof event.message !== 'string' || !event.message || event.message.length > 200) continue;
    const latest = await bindings();
    if (latest[id]?.person !== person || !allowed()) break;
    let claim;
    try { claim = await open(path + '.dispatch', 'wx', 0o600); }
    catch (error) { if (error.code === 'EEXIST') continue; throw error; }
    try {
      await send(id, event.message);
      await claim.writeFile('sent\n');
      sent++;
    } catch {
      // A send may have reached QQ despite a missing response.
      await claim.writeFile('unconfirmed\n');
      break;
    } finally {
      await claim.close();
    }
  }
  return sent;
}
