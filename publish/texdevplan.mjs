// publish/texdevplan.mjs — the cloud-texture dev feed's publish (texdev.yml),
// with the Blob calls passed in so it is tested without a store.
//
// texdev/ lives in the ICON feed's store next to the live feed, so it must
// never touch anything else: every local file, every listed blob, every upload
// and every delete is checked to be under texdev/, and anything that is not
// stops the publish before the first upload. blob.mjs is NOT used: it puts
// manifest.json at the store's root and deletes every folder the live manifest
// does not name.
//
// Order as in blob.mjs: the run's files first (cached a year — their paths
// carry the run), texdev/manifest.json last (cached 60 s), and only then the
// previous texdev run is deleted: everything listed under texdev/ that this
// publish did not write. A failed delete leaves the new manifest live and
// prints a ::warning::; the next publish deletes the rest.
import { readdir, readFile } from 'node:fs/promises';
import { join, relative, sep } from 'node:path';
import { deletePaced } from './sweep.mjs';

export const PREFIX = 'texdev/';
export const MANIFEST = 'texdev/manifest.json';
const YEAR_S = 365 * 24 * 3600;
const MANIFEST_S = 60;
const CONCURRENCY = 8;

function under(p) {
  return typeof p === 'string' && p.startsWith(PREFIX) && p.length > PREFIX.length
    && !p.split('/').some((s) => s === '..' || s === '.' || s === '');
}

function guard(p, what) {
  if (!under(p)) throw new Error(`${what} ${JSON.stringify(p)} is outside texdev/ — refusing to publish`);
  return p;
}

/**
 * localPaths: the out dir's files, relative and '/'-separated
 * ('texdev/upper/<run>/<sq>.icu', …, 'texdev/manifest.json').
 * storePaths: every blob listed under texdev/.
 * -> { uploads (manifest excluded), manifest, deletes }.
 */
export function planTexdev(localPaths, storePaths) {
  for (const p of localPaths) guard(p, 'local file');
  for (const p of storePaths) guard(p, 'listed blob');
  if (!localPaths.includes(MANIFEST)) throw new Error(`no ${MANIFEST} in the build's output`);
  const keep = new Set(localPaths);
  return {
    uploads: localPaths.filter((p) => p !== MANIFEST),
    manifest: MANIFEST,
    deletes: storePaths.filter((p) => !keep.has(p)),
  };
}

async function walk(dir, out = []) {
  for (const e of await readdir(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) await walk(p, out);
    else out.push(p);   // a link or anything else is listed too, and refused by the guard if stray
  }
  return out;
}

async function listTexdev(list, retry) {
  const out = [];
  let cursor;
  do {
    const r = await retry('list', () => list({ prefix: PREFIX, cursor, limit: 1000 }));
    for (const b of r.blobs) out.push(b.pathname);
    cursor = r.hasMore ? r.cursor : undefined;
  } while (cursor);
  return out;
}

async function pool(items, n, fn) {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (next < items.length) { const k = next++; await fn(items[k]); }
  }));
}

/** Publishes <outDir>/texdev/**. Returns { uploaded, deleted, ok }; throws only before the manifest is up. */
export async function publishTexdev({ outDir, put, list, del, retry, log = console.log, perSec }) {
  const local = (await walk(outDir)).map((p) => relative(outDir, p).split(sep).join('/'));
  const plan = planTexdev(local, await listTexdev(list, retry));
  const send = (p, maxAge, contentType) => retry(p, async () => put(guard(p, 'upload'), await readFile(join(outDir, p)), {
    access: 'public', addRandomSuffix: false, allowOverwrite: true, cacheControlMaxAge: maxAge, contentType,
  }));
  await pool(plan.uploads, CONCURRENCY, (p) => send(p, YEAR_S, 'application/octet-stream'));
  log(`uploaded ${plan.uploads.length} texdev files`);
  await send(plan.manifest, MANIFEST_S, 'application/json');
  log(`${plan.manifest} is live`);
  try {
    const deleted = await deletePaced(plan.deletes.map((p) => guard(p, 'delete')), { del, retry, perSec, log });
    log(`deleted ${deleted} files of the previous texdev run`);
    return { uploaded: plan.uploads.length + 1, deleted, ok: true };
  } catch (e) {
    log(`::warning::texdev publish: the new manifest is live, but deleting the previous run stopped `
      + `(${(e && e.message) || e}); the next publish deletes the rest`);
    return { uploaded: plan.uploads.length + 1, deleted: Number(e && e.deleted) || 0, ok: false };
  }
}
