// publish/stamp.mjs — the manifest says when it went live.
//
// builtMs is stamped when the build writes the manifest — for GFS, AIFS and
// IFS at the build's start — and the upload lands 7–38 min later (measured
// 2026-11-12, Last-Modified minus builtMs). A reader asking "was this run
// out when the photo was taken?" (SkyCast's report fill) needs the moment the
// run went live, so blob.mjs stamps it into manifest.json right before it
// uploads it, after every square is up: now plus the manifest's cache time,
// since a reader can keep the old manifest that long after the put — the
// moment EVERY reader has it, erring late, never early. Stamped on each
// attempt, so a retried upload carries its own time.
//
// publishedMs is the run's own time. A manifest that already carries one is a
// copy of the live manifest re-uploaded with an add-on — hrrr_fog_build.py
// adds `fog` to the live HRRR manifest an hour after its clouds went out — so
// the run's publishedMs is kept (its clouds were out then) and the add-on's
// time goes in republishedMs. A rebuilt run (a repair) writes a fresh manifest
// without publishedMs, so it is stamped anew. Readers that predate the keys
// ignore them.
export function stampManifest(bytes, nowMs, cacheMs = 0) {
  const m = JSON.parse(Buffer.isBuffer(bytes) ? bytes.toString('utf8') : String(bytes));
  if (!m || typeof m !== 'object' || Array.isArray(m)) throw new Error('manifest.json is not an object');
  if (!Number.isFinite(nowMs) || nowMs <= 0) throw new Error(`bad publish time ${nowMs}`);
  if (!Number.isFinite(cacheMs) || cacheMs < 0) throw new Error(`bad cache time ${cacheMs}`);
  const t = Math.floor(nowMs + cacheMs);
  if (Number.isFinite(m.publishedMs) && m.publishedMs > 0) m.republishedMs = t;
  else m.publishedMs = t;
  return Buffer.from(JSON.stringify(m), 'utf8');
}
