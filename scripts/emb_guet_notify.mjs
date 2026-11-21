import { open, readFile, readdir, stat } from 'node:fs/promises';
import { join } from 'node:path';

const eventName = /^[a-f0-9]{64}\.json$/;
const knownChannels = new Set(['数字码', 'GPS', '二维码']);
const knownStages = new Set(['started', 'action_required', 'confirmed', 'unconfirmed']);

// GUET signing events are owner-directed: there must be exactly one bound
// private chat. The user asked for these notifications from that chat, so the
// generic reminder-subscription flag is not required here; the platform still
// enforces the client-side "allow active messages" switch. Platform permission
// lifecycle events are unreliable for this bot, so delivery is proven by the
// send result instead: success is claimed, ambiguity is never retried.
export async function deliverGuetEvents({ directory, bindings, send, now = Date.now }) {
  const all = await bindings();
  const privateBindings = Object.entries(all).filter(([id, value]) =>
    /^qbot_c2c_[a-zA-Z0-9_-]+$/.test(id) && typeof value?.person === 'string' && value.person);
  if (privateBindings.length !== 1) return 0;
  const [[id]] = privateBindings;
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
    if (Object.keys(latest).filter(key => /^qbot_c2c_[a-zA-Z0-9_-]+$/.test(key) &&
        typeof latest[key]?.person === 'string' && latest[key].person).length !== 1 ||
        !latest[id]?.person) break;
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
