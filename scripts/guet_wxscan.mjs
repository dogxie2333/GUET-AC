import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const directory = resolve(process.argv[2] || '.');
const artifact = name => resolve(directory, name);
const parts = [];
let bytes = 0;
for await (const part of process.stdin) {
  bytes += part.length;
  if (bytes > 16_000_008) throw new Error('Image limit exceeded');
  parts.push(part);
}
const image = Buffer.concat(parts, bytes);
if (image.length < 9) throw new Error('Image header missing');
const width = image.readUInt32LE(0);
const height = image.readUInt32LE(4);
if (!width || !height || width * height !== image.length - 8) {
  throw new Error('Invalid image dimensions');
}

// Reuse the Apache-2.0 wxscan worker with a local Node message shim.
const runtimeWasm = await readFile(artifact('wxscan_tflite.wasm'));
const runtimeUrl = pathToFileURL(artifact('wxscan_tflite.wasm')).href;
// The published worker runtime fetches its sibling WASM using a file URL.
// Node fetch cannot read file URLs; supply only this pinned local artifact.
globalThis.fetch = async url => {
  if (String(url) !== runtimeUrl) throw new Error('Unexpected runtime URL');
  return new Response(runtimeWasm, {headers:{'Content-Type':'application/wasm'}});
};
let result;
globalThis.self = {postMessage(value) { result = value; }};
await import(pathToFileURL(artifact('wxscan_worker.mjs')));
async function request(data) {
  result = undefined;
  await self.onmessage({data});
  if (!result?.ok) throw new Error(result?.error || 'wxscan worker did not reply');
  return result;
}
const [wxscan, detect, sr] = await Promise.all(
  ['wxscan_wasm.wasm', 'detect.tflite', 'sr.tflite'].map(name => readFile(artifact(name))),
);
const ready = await request({cmd:'init', wxscan, detect, sr,
  tflite:pathToFileURL(artifact('wxscan_tflite.mjs')).href});
if (!ready.hasDetector || !ready.hasSuperResolution) {
  throw new Error('wxscan CNN models are not initialized');
}
try {
  const scan = await request({cmd:'scanGray', pixels:image.subarray(8), width, height});
  const results = JSON.parse(scan.json)?.results;
  if (!Array.isArray(results)) throw new Error('Invalid wxscan result');
  process.stdout.write(JSON.stringify(results.map(result => result.text)));
} finally {
  await request({cmd:'dispose'});
}
