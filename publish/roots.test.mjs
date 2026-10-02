import { test } from 'node:test';
import assert from 'node:assert/strict';
import { planRoots } from './roots.mjs';

test('the later root wins a path: Blob gets blob/manifest.json, which names the columns', () => {
  const { files, manifestDir } = planRoots([
    { dir: 'site', paths: ['manifest.json', 'tiles/r/N40W125.icl', 'tail/r/N40W125.icl', '.git/HEAD'] },
    { dir: 'blob', paths: ['manifest.json', 'columns/r/N40W125.icc'] },
  ]);
  assert.equal(manifestDir, 'blob');
  assert.deepEqual(files, [
    { path: 'columns/r/N40W125.icc', dir: 'blob' },
    { path: 'tail/r/N40W125.icl', dir: 'site' },
    { path: 'tiles/r/N40W125.icl', dir: 'site' },
  ]);
});

test('one root is the publish as before', () => {
  const { files, manifestDir } = planRoots([{ dir: 'site', paths: ['tiles/r/a.icl', 'manifest.json'] }]);
  assert.equal(manifestDir, 'site');
  assert.deepEqual(files, [{ path: 'tiles/r/a.icl', dir: 'site' }]);
});

test('no manifest anywhere is reported, not invented', () => {
  assert.equal(planRoots([{ dir: 'a', paths: ['x'] }, { dir: 'b', paths: [] }]).manifestDir, null);
});
