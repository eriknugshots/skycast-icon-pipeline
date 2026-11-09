// publish/store.mjs — reading a Vercel Blob feed store: every blob, and the live manifest.
// Shared by blob.mjs (the publish) and prune-backlog.mjs (the one-off cleanup).

/** Every blob in the store ({ pathname, size, url, … }), 1000 a page. */
export async function listAll(list, retry) {
  const blobs = [];
  let cursor;
  do {
    const r = await retry('list', () => list({ cursor, limit: 1000 }));
    for (const b of r.blobs) blobs.push(b);
    cursor = r.hasMore ? r.cursor : undefined;
  } while (cursor);
  return blobs;
}

/** The live manifest.json in a listing, fetched past every cache; null when absent or unreadable. */
export async function readManifest(blobs, fetchFn = fetch) {
  const b = blobs.find((x) => x.pathname === 'manifest.json');
  if (!b) return null;
  try {
    const r = await fetchFn(`${b.url}?t=${Date.now()}`, { cache: 'no-store' });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}
