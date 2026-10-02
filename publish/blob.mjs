// publish/blob.mjs — copy a built site/ to SkyCast's Vercel Blob feed store.
//
// Erik, 2026-10-01: serve the feed from Vercel (Pro) instead of GitHub Pages,
// whose hard 1 GB cap is why the feed once left the open ocean out. Pages stays
// published too: builds already on testers' phones read it.
//
// Order matters. The run's folders (tiles/<run>/, tail/<run>/) are uploaded
// first — their paths never change, so they are cached for a year — and
// manifest.json LAST, cached 60 s, so no phone ever reads a manifest whose
// squares are not there yet. Then every folder but the new manifest's and the
// previous manifest's is deleted (prune.mjs): a phone that read the old
// manifest a moment ago is still fetching its squares.
//
// Several roots may be given: `node blob.mjs ../site ../blob` uploads site/
// (what Pages carries too) and then blob/ (Blob only: the column feed and the
// manifest that names it); a later root's file replaces the same path in an
// earlier one (roots.mjs), so the manifest that goes up is blob/'s.
//
// Usage: BLOB_READ_WRITE_TOKEN=… node publish/blob.mjs <siteDir> [<moreDir> …]
import { readdir, readFile } from 'node:fs/promises';
import { join, relative, sep } from 'node:path';
import { put, list, del } from '@vercel/blob';
import { pathsToDelete } from './prune.mjs';
import { planRoots } from './roots.mjs';

const YEAR_S = 365 * 24 * 3600;
const MANIFEST_S = 60;            // Blob's shortest cache time
const CONCURRENCY = 16;           // Pro allows 75 uploads/s; leave room for retries
const ATTEMPTS = 4;

async function walk(dir) {
  const out = [];
  for (const e of await readdir(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) out.push(...await walk(p));
    else if (e.isFile()) out.push(p);
  }
  return out;
}

async function retry(what, fn) {
  for (let i = 1; ; i++) {
    try { return await fn(); } catch (e) {
      if (i >= ATTEMPTS) throw new Error(`${what}: ${e.message || e}`);
      await new Promise((r) => setTimeout(r, 500 * 2 ** i));
    }
  }
}

async function pool(items, n, fn) {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (next < items.length) { const k = next++; await fn(items[k], k); }
  }));
}

async function listAll() {
  const blobs = [];
  let cursor;
  do {
    const r = await retry('list', () => list({ cursor, limit: 1000 }));
    blobs.push(...r.blobs);
    cursor = r.hasMore ? r.cursor : undefined;
  } while (cursor);
  return blobs;
}

async function previousManifest(blobs) {
  const b = blobs.find((x) => x.pathname === 'manifest.json');
  if (!b) return null;
  try {
    const r = await fetch(`${b.url}?t=${Date.now()}`, { cache: 'no-store' });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

async function main() {
  const dirs = process.argv.slice(2);
  if (!dirs.length) throw new Error('usage: node publish/blob.mjs <siteDir> [<moreDir> …]');
  if (!process.env.BLOB_READ_WRITE_TOKEN) throw new Error('BLOB_READ_WRITE_TOKEN is not set');
  const roots = [];
  for (const dir of dirs) {
    roots.push({ dir, paths: (await walk(dir)).map((p) => relative(dir, p).split(sep).join('/')) });
  }
  const { files, manifestDir } = planRoots(roots);
  if (!manifestDir) throw new Error(`no manifest.json in ${dirs.join(', ')}`);
  const current = JSON.parse(await readFile(join(manifestDir, 'manifest.json'), 'utf8'));
  const before = await listAll();
  const previous = await previousManifest(before);

  const t0 = Date.now();
  const bytesByDir = Object.fromEntries(dirs.map((d) => [d, 0]));
  const countByDir = Object.fromEntries(dirs.map((d) => [d, 0]));
  await pool(files, CONCURRENCY, async ({ path, dir }) => {
    const body = await readFile(join(dir, path));
    bytesByDir[dir] += body.length;
    countByDir[dir] += 1;
    await retry(path, () => put(path, body, {
      access: 'public', addRandomSuffix: false, allowOverwrite: true,
      cacheControlMaxAge: YEAR_S, contentType: 'application/octet-stream',
    }));
  });
  const bytes = Object.values(bytesByDir).reduce((a, b) => a + b, 0);
  console.log(`uploaded ${files.length} files, ${(bytes / 1e6).toFixed(0)} MB in ${((Date.now() - t0) / 1000).toFixed(0)} s (`
    + dirs.map((d) => `${d}: ${countByDir[d]} files, ${(bytesByDir[d] / 1e6).toFixed(0)} MB`).join('; ') + ')');

  await retry('manifest.json', async () => put('manifest.json', await readFile(join(manifestDir, 'manifest.json')), {
    access: 'public', addRandomSuffix: false, allowOverwrite: true,
    cacheControlMaxAge: MANIFEST_S, contentType: 'application/json',
  }));
  console.log(`manifest.json → run ${current.run}`);

  const doomed = pathsToDelete((await listAll()).map((b) => b.pathname), { current, previous });
  for (let i = 0; i < doomed.length; i += 1000) {
    const batch = doomed.slice(i, i + 1000);
    await retry('delete', () => del(batch));
  }
  console.log(`deleted ${doomed.length} files from older runs`);
}

main().catch((e) => { console.error(`::error::blob publish failed: ${e.message || e}`); process.exit(1); });
