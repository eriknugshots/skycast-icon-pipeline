// publish/prune-backlog.mjs — one-off cleanup of a feed store's old runs.
//
// From Nov 1 every publish's delete phase hit Blob's rate limit and gave up, so
// no old run was ever deleted (the ICON store reached 90.8 GB and 320,700 files
// for about 9 GB of live runs). blob.mjs now paces its deletes and its next
// prune would catch up, but clearing 300,000 files inside a build's time budget
// is a poor idea. This clears a store on its own.
//
// It reads the LIVE manifest from the store and keeps, per folder family, the
// run it names, anything newer (a build may be uploading right now) and the run
// before it (prune.mjs backlogToDelete); everything else goes, paced as the
// publish paces it (sweep.mjs). Loose files such as manifest.json stay.
//
// Usage: BLOB_READ_WRITE_TOKEN=… node publish/prune-backlog.mjs [--dry-run]
//   --dry-run  print files and bytes per folder and per run, kept and deleted; delete nothing
// Workflow: .github/workflows/prune-backlog.yml (pick the store; dry_run defaults to true).
import { pathToFileURL } from 'node:url';
import { list, del } from '@vercel/blob';
import { createRetry } from './pace.mjs';
import { backlogToDelete, summarize } from './prune.mjs';
import { deletePaced, DELETES_PER_SEC } from './sweep.mjs';
import { listAll, readManifest } from './store.mjs';

const fmt = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`);

/** The report lines for one set of blobs. */
export function reportLines(title, blobs) {
  const s = summarize(blobs);
  const out = [`${title}: ${s.total.files} files, ${fmt(s.total.bytes)}`];
  for (const [f, v] of s.families) out.push(`  ${f}/  ${v.files} files, ${fmt(v.bytes)}`);
  for (const [r, v] of s.runs) out.push(`    ${r}/  ${v.files} files, ${fmt(v.bytes)}`);
  return out;
}

/**
 * The whole cleanup with its I/O passed in, so a test can run it on a fake
 * store. Returns { kept, doomed, deleted }.
 */
export async function pruneBacklog({ list: listFn, del: delFn, fetch: fetchFn, retry, dryRun, perSec, log = console.log, sweep = {} }) {
  const blobs = await listAll(listFn, retry);
  const manifest = await readManifest(blobs, fetchFn);
  if (!manifest) throw new Error('no readable manifest.json in the store; refusing to prune');
  const gone = new Set(backlogToDelete(blobs.map((b) => b.pathname), manifest));
  const doomed = blobs.filter((b) => gone.has(b.pathname));
  const kept = blobs.filter((b) => !gone.has(b.pathname));
  log(`live manifest: run ${manifest.run}`);
  for (const l of reportLines('keep', kept)) log(l);
  for (const l of reportLines(dryRun ? 'would delete' : 'delete', doomed)) log(l);
  if (dryRun) { log('dry run: nothing deleted'); return { kept, doomed, deleted: 0 }; }
  const t0 = Date.now();
  const deleted = await deletePaced(doomed.map((b) => b.pathname), { del: delFn, retry, perSec, log, ...sweep });
  log(`deleted ${deleted} files, ${fmt(doomed.reduce((a, b) => a + (Number(b.size) || 0), 0))} in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
  return { kept, doomed, deleted };
}

async function main() {
  const args = process.argv.slice(2);
  const bad = args.filter((a) => a !== '--dry-run');
  if (bad.length) throw new Error(`unknown argument ${bad[0]}; usage: node publish/prune-backlog.mjs [--dry-run]`);
  if (!process.env.BLOB_READ_WRITE_TOKEN) throw new Error('BLOB_READ_WRITE_TOKEN is not set');
  await pruneBacklog({
    list, del, fetch, retry: createRetry({ attempts: 4 }),
    dryRun: args.includes('--dry-run'),
    perSec: Number(process.env.BLOB_DELETES_PER_SEC) || DELETES_PER_SEC,
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((e) => {
    const done = Number(e && e.deleted) || 0;
    console.error(`::error::prune-backlog failed${done ? ` after deleting ${done} files` : ''}: ${e.message || e}`);
    process.exit(1);
  });
}
