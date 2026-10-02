// publish/roots.mjs — which file goes where when a publish has several roots. Pure.
//
// blob.mjs uploads `site/` (what Pages carries too: squares, tail, a manifest
// naming no columns) and then `blob/` (Blob only: the column feed and the
// manifest that names it). A path in a later root replaces the same path in
// an earlier one, so Blob's manifest.json is blob/'s.

/**
 * roots: [{ dir, paths }] in order, paths relative and '/'-separated.
 * Returns { files: [{ path, dir }] (manifest.json left out, sorted by path),
 *           manifestDir: the dir whose manifest.json wins, or null }.
 */
export function planRoots(roots) {
  const from = new Map();
  for (const { dir, paths } of roots) {
    for (const p of paths) {
      if (p.startsWith('.git/')) continue;
      from.set(p, dir);
    }
  }
  const manifestDir = from.has('manifest.json') ? from.get('manifest.json') : null;
  const files = [...from.entries()]
    .filter(([p]) => p !== 'manifest.json')
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([path, dir]) => ({ path, dir }));
  return { files, manifestDir };
}
