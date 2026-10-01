// publish/prune.mjs — which blobs a publish may delete. Pure.
//
// The feed's blob store holds manifest.json plus one folder per run (tiles/<run>/)
// and per tail (tail/<run>/). A publish keeps the folders the NEW manifest names
// and the ones the PREVIOUS manifest named — a phone that read the old manifest a
// moment ago is still fetching its squares — and deletes every other folder.

/** The folders a manifest points at, each ending in '/'. */
export function manifestFolders(m) {
  const out = [];
  if (m && typeof m.tiles === 'string' && m.tiles) out.push(m.tiles.replace(/\/?$/, '/'));
  if (m && m.tail && typeof m.tail.tiles === 'string' && m.tail.tiles) out.push(m.tail.tiles.replace(/\/?$/, '/'));
  return out;
}

/** Pathnames to delete: everything but manifest.json and the kept folders. */
export function pathsToDelete(pathnames, { current, previous }) {
  const keep = [...manifestFolders(current), ...manifestFolders(previous)];
  return pathnames.filter((p) => p !== 'manifest.json' && !keep.some((k) => p.startsWith(k)));
}
