import dns from 'node:dns';
import { syncBuiltinESMExports } from 'node:module';
import tls from 'node:tls';

const original = dns.lookup;
const qqHosts = new Set(['bots.qq.com','api.sgroup.qq.com']);
const preferred = new Map();

function reachable(host, candidate) {
  return new Promise(resolve => {
    const socket = tls.connect({
      host: candidate.address, port: 443, servername: host,
      rejectUnauthorized: true, family: candidate.family,
    });
    const done = ok => { socket.destroy(); resolve(ok); };
    socket.setTimeout(1600, () => done(false));
    socket.once('error', () => done(false));
    socket.once('secureConnect', () => done(true));
  });
}

dns.lookup = function(host, options, callback) {
  if (!qqHosts.has(host)) return original.apply(this, arguments);
  if (typeof options === 'function') { callback = options; options = {}; }
  if (typeof options === 'number') options = {family:options};
  options ||= {};
  return original.call(this, host, {...options,all:true}, (error, addresses) => {
    if (error) return callback(error);
    const finish = list => options.all
      ? callback(null,list)
      : callback(null,list[0]?.address,list[0]?.family);
    if (addresses.length < 2) return finish(addresses);
    const cached = preferred.get(host);
    const first = addresses.find(a => a.address === cached?.address && cached.expires > Date.now());
    if (first) return finish([first,...addresses.filter(a => a !== first)]);
    Promise.any(addresses.map(async address => {
      if (await reachable(host,address)) return address;
      throw new Error('QQ edge unreachable');
    })).then(address => {
      preferred.set(host,{address:address.address,expires:Date.now()+15_000});
      finish([address,...addresses.filter(a => a !== address)]);
    }, () => finish(addresses));
  });
};
syncBuiltinESMExports();

const builtinFetch = globalThis.fetch;
globalThis.fetch = async function(input, init) {
  // SDK uses string URLs; leave every other fetch, including QQ sends, alone.
  const token = input === 'https://bots.qq.com/app/getAppAccessToken' && init?.method === 'POST';
  const gateway = input === 'https://api.sgroup.qq.com/gateway' &&
    (!init?.method || init.method === 'GET');
  if (!token && !gateway) return builtinFetch(input, init);
  let failure;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (init?.signal?.aborted) throw init.signal.reason;
    const deadline = AbortSignal.timeout(2800);
    const signal = init?.signal ? AbortSignal.any([init.signal,deadline]) : deadline;
    try {
      const response = await builtinFetch(input,{...init,signal,redirect:'error'});
      console.log('QQ_EDGE_HTTP', token ? 'token' : 'gateway', response.status);
      return response;
    } catch (error) {
      const code = error?.cause?.code;
      if (init?.signal?.aborted ||
          (!deadline.aborted && !['UND_ERR_CONNECT_TIMEOUT','ECONNRESET','ETIMEDOUT','EHOSTUNREACH'].includes(code))) {
        throw error;
      }
      failure = error;
      preferred.delete(token ? 'bots.qq.com' : 'api.sgroup.qq.com');
      console.warn('QQ_EDGE_RETRY',token ? 'token' : 'gateway',attempt+1);
    }
  }
  throw failure;
};
