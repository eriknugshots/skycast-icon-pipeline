import { test } from 'node:test';
import assert from 'node:assert/strict';
import { manifestFolders, pathsToDelete } from './prune.mjs';

const cur = { tiles: 'tiles/2026100200', tail: { tiles: 'tail/2026100200' } };
const prev = { tiles: 'tiles/2026100118', tail: { tiles: 'tail/2026100112' } };

test('the folders a manifest names', () => {
  assert.deepEqual(manifestFolders(cur), ['tiles/2026100200/', 'tail/2026100200/']);
  assert.deepEqual(manifestFolders({ tiles: 'tiles/x/' }), ['tiles/x/']);
  assert.deepEqual(manifestFolders(null), []);
});

test('keeps the manifest, the current run and the previous one; deletes older runs', () => {
  const paths = [
    'manifest.json',
    'tiles/2026100200/N40W125.icl', 'tail/2026100200/N40W125.icl',
    'tiles/2026100118/N40W125.icl', 'tail/2026100112/N40W125.icl',
    'tiles/2026100112/N40W125.icl', 'tail/2026100100/N40W125.icl',
    'tiles/20261002000/N00E000.icl',   // a prefix lookalike is not the same folder
  ];
  assert.deepEqual(pathsToDelete(paths, { current: cur, previous: prev }), [
    'tiles/2026100112/N40W125.icl', 'tail/2026100100/N40W125.icl', 'tiles/20261002000/N00E000.icl',
  ]);
});

test('with no previous manifest only the current run is kept', () => {
  assert.deepEqual(pathsToDelete(['manifest.json', 'tiles/a/x.icl', 'tiles/b/x.icl'], { current: { tiles: 'tiles/b' }, previous: null }), ['tiles/a/x.icl']);
});

test('the column feed folder is kept like the tail; a null or old-style manifest has none', () => {
  const withCols = { ...cur, columns: { tiles: 'columns/2026100200' } };
  const prevCols = { ...prev, columns: { tiles: 'columns/2026100118' } };
  assert.deepEqual(manifestFolders(withCols), ['tiles/2026100200/', 'tail/2026100200/', 'columns/2026100200/']);
  assert.deepEqual(manifestFolders({ ...cur, columns: null }), manifestFolders(cur));
  const paths = ['columns/2026100200/N40W125.icc', 'columns/2026100118/N40W125.icc', 'columns/2026100112/N40W125.icc'];
  assert.deepEqual(pathsToDelete(paths, { current: withCols, previous: prevCols }), ['columns/2026100112/N40W125.icc']);
  // A build that published no columns deletes the older column folders.
  assert.deepEqual(pathsToDelete(paths, { current: cur, previous: prev }), paths);
});
