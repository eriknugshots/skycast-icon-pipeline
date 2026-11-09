// publish/sweep.mjs — delete a list of blobs without tripping Blob's rate limit.
//
// From Nov 1 every ICON build ended on "delete: Vercel Blob: Too many requests
// please lower the number of concurrent requests": blob.mjs deleted the old runs
// in batches of 1000, back to back, and the error carried no retryAfter, so the
// retry gave up after four short backoffs and NOTHING old was ever deleted (the
// store reached 90.8 GB and 320,700 files for about 9 GB of live runs).
//
// Now: small batches, paced to a deletes-per-second budget, rate limits waited
// out (pace.mjs createRetry), a progress line every `every` files.

import { paceDelay } from './pace.mjs';

export const DELETE_BATCH = 100;
export const DELETES_PER_SEC = 100;

/**
 * Deletes `paths` with del(batch) through retry(what, fn). Returns the number
 * deleted. On a failure the error is rethrown with `.deleted` (how many went
 * before it) so a caller can report it.
 *
 * Pacing restarts from the last batch that went through when the sweep has
 * fallen more than a second behind (after a rate-limit wait): catching up on
 * the missed schedule would be the very burst that drew the limit.
 */
export async function deletePaced(paths, {
  del, retry,
  batch = DELETE_BATCH, perSec = DELETES_PER_SEC,
  now = () => Date.now(), sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
  log = console.log, every = 10000,
} = {}) {
  let t0 = now(), base = 0, done = 0, nextLog = every, lastAt = t0;
  for (let i = 0; i < paths.length; i += batch) {
    if (perSec > 0 && i > 0 && now() - (t0 + ((i - base) / perSec) * 1000) > 1000) { t0 = lastAt; base = i - batch; }
    const wait = paceDelay(i - base, perSec, t0, now());
    if (wait > 0) await sleep(wait);
    const b = paths.slice(i, i + batch);
    try {
      await retry('delete', () => del(b));
    } catch (e) {
      const err = e instanceof Error ? e : new Error(String(e));
      err.deleted = done;
      throw err;
    }
    lastAt = now();
    done += b.length;
    if (every > 0 && done >= nextLog && done < paths.length) {
      log(`deleted ${done} of ${paths.length} files…`);
      while (nextLog <= done) nextLog += every;
    }
  }
  return done;
}

/**
 * blob.mjs's last phase: delete every run the new and the previous manifest do
 * not name (prune.mjs pathsToDelete over the FULL listing, so a backlog left by
 * earlier failed prunes goes too). Never throws: the new manifest is already
 * live, so a failure here prints a ::warning:: and leaves the rest to the next
 * publish's prune. Returns { doomed, deleted, ok }.
 */
export async function pruneOldRuns({ listAll, pathsToDelete, current, previous, del, retry, perSec, log = console.log, sweep = {} }) {
  let doomed = [];
  const t0 = Date.now();
  try {
    doomed = pathsToDelete((await listAll()).map((b) => b.pathname), { current, previous });
    const deleted = await deletePaced(doomed, { del, retry, perSec, log, ...sweep });
    log(`deleted ${deleted} files from older runs in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
    return { doomed, deleted, ok: true };
  } catch (e) {
    const deleted = Number(e && e.deleted) || 0;
    log(`::warning::blob publish: run ${current && current.run} is live, but pruning older runs stopped after ${deleted} of ${doomed.length} files `
      + `(${(e && e.message) || e}); the next publish's prune picks up the rest`);
    return { doomed, deleted, ok: false };
  }
}
