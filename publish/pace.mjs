// publish/pace.mjs — how a large publish stays inside Vercel Blob's rate limit. Pure.
//
// The first place index upload reached 26,500 files at about 90 a second, and
// then Blob answered "Too many requests ... try again in 60 seconds". A rate
// limit is not a failure: every worker waits out the time Blob named, and the
// wait never uses up an attempt.

/** Retry fn: rate limits wait as long as Blob says, shared by all workers; other errors use up `attempts`. */
export function createRetry({ attempts = 4, sleep = (ms) => new Promise((r) => setTimeout(r, ms)), now = () => Date.now(), maxRateWaits = 60 } = {}) {
  let resumeAt = 0;
  return async function retry(what, fn) {
    let tries = 0, rateWaits = 0;
    for (;;) {
      const wait = resumeAt - now();
      if (wait > 0) await sleep(wait);
      try { return await fn(); } catch (e) {
        const after = Number(e && e.retryAfter);
        if (Number.isFinite(after) && after > 0) {
          rateWaits += 1;
          if (rateWaits > maxRateWaits) throw new Error(`${what}: still rate limited: ${e.message || e}`);
          resumeAt = Math.max(resumeAt, now() + after * 1000);
          continue;
        }
        tries += 1;
        if (tries >= attempts) throw new Error(`${what}: ${e.message || e}`);
        await sleep(500 * 2 ** tries);
      }
    }
  };
}

/** Ms to wait so upload number k (from 0) starts no faster than perSec; 0 with no limit. */
export function paceDelay(k, perSec, startMs, nowMs) {
  if (!(perSec > 0)) return 0;
  return Math.max(0, startMs + (k / perSec) * 1000 - nowMs);
}

/**
 * The files still to send. With skip, a path already in the store is left, for
 * a re-run of a build whose folder is named by its date (places.yml): the same
 * day's build holds the same rows. index.json always goes up.
 */
export function filesToSend(files, existing, skip) {
  if (!skip) return files;
  const have = new Set(existing);
  return files.filter(({ path }) => !have.has(path) || path.endsWith('/index.json'));
}
