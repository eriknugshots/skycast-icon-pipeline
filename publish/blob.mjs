// publish/blob.mjs — copy a built site/ to SkyCast's Vercel Blob feed store.
//
// Erik, 2026-10-01: serve the feed from Vercel (Pro) instead of GitHub Pages,
// whose hard 1 GB cap is why the feed once left the open ocean out. Pages stays
// published too: builds already on testers' phones read it.
//
// Order matters. The run's folders (tiles, blocks, tail, columns, thick and fog, each
// <folder>/<run>/ — the ones a manifest names; prune.mjs is the source of truth)
// are uploaded first — their paths never change, so they are cached for a year — and
// manifest.json LAST, cached 60 s, so no phone ever reads a manifest whose
// squares are not there yet. Then every folder but the new manifest's and the
// previous manifest's is deleted (prune.mjs): a phone that read the old
// manifest a moment ago is still fetching its squares. The deletes are paced
// (sweep.mjs) and a failure among them does NOT fail the publish: the new
// manifest is live by then, and the next publish's prune lists the whole store
// and picks up whatever is left (a ::warning:: says so).
//
// Several roots may be given: `node blob.mjs ../site ../blob` uploads site/
// (what Pages carries too) and then blob/ (Blob only: blocks, thickness and the
// column feed, with the manifest that names them); a later root's file replaces the same path in an
// earlier one (roots.mjs), so the manifest that goes up is blob/'s.
//
// Usage: BLOB_READ_WRITE_TOKEN=… node publish/blob.mjs <siteDir> [<moreDir> …]
import { readdir, readFile } from 'node:fs/promises';
import { join, relative, sep } from 'node:path';
import { put, list, del } from '@vercel/blob';
import { pathsToDelete } from './prune.mjs';
import { planRoots } from './roots.mjs';
import { createRetry, paceDelay, filesToSend } from './pace.mjs';
import { pruneOldRuns, DELETES_PER_SEC } from './sweep.mjs';
import { listAll as listStore, readManifest } from './store.mjs';

const YEAR_S = 365 * 24 * 3600;
const MANIFEST_S = 60;            // Blob's shortest cache time
const CONCURRENCY = 16;           // Pro allows 75 uploads/s; leave room for retries
const ATTEMPTS = 4;

// One shared array, never push(...walk(p)): a spread of a whole folder's files
// overflows the stack at about 125,000 (the place index has 130,000).
async function walk(dir, out = []) {
  for (const e of await readdir(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) await walk(p, out);
    else if (e.isFile()) out.push(p);
  }
  return out;
}

const retry = createRetry({ attempts: ATTEMPTS });
// Uploads a second, for a publish too big for Blob's limit (places.yml); unset means no limit.
const PER_SEC = Number(process.env.BLOB_UPLOADS_PER_SEC) || 0;

async function pool(items, n, fn) {
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (next < items.length) { const k = next++; await fn(items[k], k); }
  }));
}

// Deletes a second (sweep.mjs): 100 a batch, so 1 call a second by default.
const DEL_PER_SEC = Number(process.env.BLOB_DELETES_PER_SEC) || DELETES_PER_SEC;

const listAll = () => listStore(list, retry);

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
  const previous = await readManifest(before);

  const t0 = Date.now();
  const bytesByDir = Object.fromEntries(dirs.map((d) => [d, 0]));
  const countByDir = Object.fromEntries(dirs.map((d) => [d, 0]));
  const send = filesToSend(files, before.map((b) => b.pathname), process.env.BLOB_SKIP_EXISTING === '1');
  if (send.length !== files.length) console.log(`resuming: ${files.length - send.length} files already in the store`);
  await pool(send, CONCURRENCY, async ({ path, dir }, k) => {
    const wait = paceDelay(k, PER_SEC, t0, Date.now());
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    const body = await readFile(join(dir, path));
    bytesByDir[dir] += body.length;
    countByDir[dir] += 1;
    await retry(path, () => put(path, body, {
      access: 'public', addRandomSuffix: false, allowOverwrite: true,
      cacheControlMaxAge: YEAR_S, contentType: 'application/octet-stream',
    }));
  });
  const bytes = Object.values(bytesByDir).reduce((a, b) => a + b, 0);
  console.log(`uploaded ${send.length} files, ${(bytes / 1e6).toFixed(0)} MB in ${((Date.now() - t0) / 1000).toFixed(0)} s (`
    + dirs.map((d) => `${d}: ${countByDir[d]} files, ${(bytesByDir[d] / 1e6).toFixed(0)} MB`).join('; ') + ')');

  await retry('manifest.json', async () => put('manifest.json', await readFile(join(manifestDir, 'manifest.json')), {
    access: 'public', addRandomSuffix: false, allowOverwrite: true,
    cacheControlMaxAge: MANIFEST_S, contentType: 'application/json',
  }));
  console.log(`manifest.json → run ${current.run}`);

  // From here on the publish has worked: the new run is live. A failed prune
  // leaves old files for the next publish's prune, never a red build.
  await pruneOldRuns({ listAll, pathsToDelete, current, previous, del, retry, perSec: DEL_PER_SEC });
}

main().catch((e) => { console.error(`::error::blob publish failed: ${e.message || e}`); process.exit(1); });
