// publish/prune.mjs — which blobs a publish may delete. Pure.
//
// The feed's blob store holds manifest.json plus one folder per run (tiles/<run>/),
// per block file set (blocks/<run>/), per tail (tail/<run>/), per column feed
// (columns/<run>/), per thickness feed (thick/<run>/) and per fog feed (fog/<run>/). A publish keeps the
// folders the NEW manifest names and the ones the PREVIOUS manifest named — a phone
// that read the old manifest a moment ago is still fetching its squares — and
// deletes every other folder.

/** The folders a manifest points at, each ending in '/'. */
export function manifestFolders(m) {
  const out = [];
  // tiles and blocks name their folder directly; the rest are objects that carry it as .tiles.
  for (const k of ['tiles', 'blocks']) {
    if (m && typeof m[k] === 'string' && m[k]) out.push(m[k].replace(/\/?$/, '/'));
  }
  for (const k of ['tail', 'columns', 'thick', 'fog']) {
    if (m && m[k] && typeof m[k].tiles === 'string' && m[k].tiles) out.push(m[k].tiles.replace(/\/?$/, '/'));
  }
  return out;
}

/** Pathnames to delete: everything but manifest.json and the kept folders. */
export function pathsToDelete(pathnames, { current, previous }) {
  const keep = [...manifestFolders(current), ...manifestFolders(previous)];
  return pathnames.filter((p) => p !== 'manifest.json' && !keep.some((k) => p.startsWith(k)));
}

// The one-off backlog cleanup (prune-backlog.mjs). From Nov 1 every publish's
// delete phase failed, so the stores hold many runs. pathsToDelete above already
// catches up on such a backlog on the next publish that gets through its deletes
// (it works from the full listing, not from the previous manifest), but a store
// of 320,000 files is worth clearing on its own, without a build behind it.

/** 'tiles/2026100200/N40W125.icl' -> { family: 'tiles', run: '2026100200' }; null for a loose file. */
export function runFolder(pathname) {
  const parts = pathname.split('/');
  if (parts.length < 3 || !parts[0] || !parts[1]) return null;
  return { family: parts[0], run: parts[1] };
}

/**
 * Pathnames the backlog cleanup deletes, given the LIVE manifest. Per family
 * (tiles, blocks, tail, columns, thick, or any other top-level folder):
 *  - the folder the manifest names, and every folder sorting AFTER it (a build
 *    may be uploading its next run right now), are kept;
 *  - so is the newest folder before it: the run before the current one, which
 *    a phone that read the previous manifest may still be fetching;
 *  - everything older is deleted.
 * A family the manifest does not name is measured against the manifest's own
 * tiles run. Loose files (manifest.json, anything not under <family>/<run>/)
 * are never deleted. A manifest naming no tiles folder deletes nothing: it
 * throws, since every folder would otherwise look old.
 */
export function backlogToDelete(pathnames, manifest) {
  const named = new Map();
  for (const f of manifestFolders(manifest)) {
    const r = runFolder(f + 'x');
    if (r) named.set(r.family, r.run);
  }
  const currentRun = named.get('tiles');
  if (!currentRun) throw new Error('the live manifest names no tiles folder; refusing to prune');
  const runs = new Map();     // family -> Set of runs
  for (const p of pathnames) {
    const r = runFolder(p);
    if (!r) continue;
    if (!runs.has(r.family)) runs.set(r.family, new Set());
    runs.get(r.family).add(r.run);
  }
  const keep = new Map();     // family -> Set of runs kept
  for (const [family, set] of runs) {
    const bound = named.get(family) ?? currentRun;
    const kept = new Set([...set].filter((r) => r >= bound));
    const older = [...set].filter((r) => r < bound).sort();
    if (older.length) kept.add(older[older.length - 1]);
    keep.set(family, kept);
  }
  return pathnames.filter((p) => {
    const r = runFolder(p);
    return r && !keep.get(r.family).has(r.run);
  });
}

/**
 * Counts and bytes of `blobs` ({ pathname, size }) by family and by family/run,
 * both sorted by name: { total: { files, bytes }, families: [[family, { files, bytes }]],
 * runs: [['tiles/2026100200', { files, bytes }]] }. Loose files count as family '.'.
 */
export function summarize(blobs) {
  const fam = new Map(), run = new Map();
  const total = { files: 0, bytes: 0 };
  const add = (m, k, size) => {
    const v = m.get(k) || { files: 0, bytes: 0 };
    v.files += 1; v.bytes += size; m.set(k, v);
  };
  for (const b of blobs) {
    const size = Number(b.size) || 0;
    const r = runFolder(b.pathname);
    add(fam, r ? r.family : '.', size);
    if (r) add(run, `${r.family}/${r.run}`, size);
    total.files += 1; total.bytes += size;
  }
  const sorted = (m) => [...m.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return { total, families: sorted(fam), runs: sorted(run) };
}
