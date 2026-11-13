// publish/stamp.mjs — the manifest says when it went live.
//
// builtMs is stamped when the build writes the manifest — for GFS, AIFS and
// IFS at the build's start — and the upload lands 7–38 min later (measured
// 2026-11-12, Last-Modified minus builtMs). A reader asking "was this run
// out when the photo was taken?" (SkyCast's report fill) needs the moment the
// run went live, so blob.mjs stamps publishedMs into manifest.json right
// before it uploads it, after every square is up. Stamped on each attempt, so
// a retried upload carries its own time; the manifest becomes readable a
// moment after the stamp (the put's own time), never minutes. Readers that
// predate the key ignore it.
export function stampManifest(bytes, nowMs) {
  const m = JSON.parse(Buffer.isBuffer(bytes) ? bytes.toString('utf8') : String(bytes));
  if (!m || typeof m !== 'object' || Array.isArray(m)) throw new Error('manifest.json is not an object');
  if (!Number.isFinite(nowMs) || nowMs <= 0) throw new Error(`bad publish time ${nowMs}`);
  m.publishedMs = Math.floor(nowMs);
  return Buffer.from(JSON.stringify(m), 'utf8');
}
