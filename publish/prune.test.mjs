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
