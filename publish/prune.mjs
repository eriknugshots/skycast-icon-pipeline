// publish/prune.mjs — which blobs a publish may delete. Pure.
//
// The feed's blob store holds manifest.json plus one folder per run (tiles/<run>/),
// per block file set (blocks/<run>/), per tail (tail/<run>/), per column feed
// (columns/<run>/) and per thickness feed (thick/<run>/). A publish keeps the
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
  for (const k of ['tail', 'columns', 'thick']) {
    if (m && m[k] && typeof m[k].tiles === 'string' && m[k].tiles) out.push(m[k].tiles.replace(/\/?$/, '/'));
  }
  return out;
}

/** Pathnames to delete: everything but manifest.json and the kept folders. */
export function pathsToDelete(pathnames, { current, previous }) {
  const keep = [...manifestFolders(current), ...manifestFolders(previous)];
  return pathnames.filter((p) => p !== 'manifest.json' && !keep.some((k) => p.startsWith(k)));
}
