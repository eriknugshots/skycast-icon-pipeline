import { test } from 'node:test';
import assert from 'node:assert/strict';
import { manifestFolders, pathsToDelete, runFolder, backlogToDelete, summarize } from './prune.mjs';

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

test('the blocks and thickness folders are kept like the others; a manifest without them has none', () => {
  const m = { tiles: 'tiles/A', blocks: 'blocks/A', thick: { tiles: 'thick/A' }, tail: null, columns: null };
  assert.deepEqual(manifestFolders(m), ['tiles/A/', 'blocks/A/', 'thick/A/']);
  assert.deepEqual(manifestFolders({ tiles: 'tiles/A', blocks: null, thick: null }), ['tiles/A/']);
  const old = { tiles: 'tiles/Z' };
  const paths = ['blocks/A/x.icb', 'thick/A/x.ict', 'tiles/Z/x.icl', 'blocks/Q/x.icb', 'thick/Q/x.ict'];
  assert.deepEqual(pathsToDelete(paths, { current: m, previous: old }), ['blocks/Q/x.icb', 'thick/Q/x.ict']);
});

// From Nov 1 no prune got through its deletes, so a store held a backlog of
// many runs. The next prune that works must clear ALL of it, not just the run
// the previous manifest replaced: pathsToDelete works from the full listing.
test('a prune after failed ones clears the whole backlog, every family', () => {
  const runs = ['2026103000', '2026103006', '2026103012', '2026103018', '2026103100', '2026110100', '2026110106', '2026110112'];
  const fams = ['tiles', 'blocks', 'tail', 'columns', 'thick'];
  const paths = ['manifest.json'];
  for (const r of runs) for (const f of fams) paths.push(`${f}/${r}/N40W125.x`, `${f}/${r}/S10E020.x`);
  const m = (r) => ({ tiles: `tiles/${r}`, blocks: `blocks/${r}`, tail: { tiles: `tail/${r}` }, columns: { tiles: `columns/${r}` }, thick: { tiles: `thick/${r}` } });
  const doomed = pathsToDelete(paths, { current: m('2026110112'), previous: m('2026110106') });
  assert.equal(doomed.length, 6 * fams.length * 2);
  const left = paths.filter((p) => !doomed.includes(p));
  assert.deepEqual(left.sort(), ['manifest.json', ...fams.flatMap((f) => ['2026110106', '2026110112'].flatMap((r) => [`${f}/${r}/N40W125.x`, `${f}/${r}/S10E020.x`]))].sort());
});

test('runFolder', () => {
  assert.deepEqual(runFolder('tiles/2026100200/N40W125.icl'), { family: 'tiles', run: '2026100200' });
  assert.equal(runFolder('manifest.json'), null);
  assert.equal(runFolder('tiles/x'), null);
});

test('backlog: keeps the live run, the one before it and anything newer, per family; deletes the rest', () => {
  const live = { tiles: 'tiles/2026110112', blocks: 'blocks/2026110112', tail: { tiles: 'tail/2026110100' }, columns: null, thick: { tiles: 'thick/2026110112' } };
  const paths = [
    'manifest.json', 'robots.txt',
    'tiles/2026103100/a', 'tiles/2026110100/a', 'tiles/2026110106/a', 'tiles/2026110112/a', 'tiles/2026110118/a',  // 18Z: uploading now
    'blocks/2026110100/a', 'blocks/2026110112/a',             // no 06Z blocks: 00Z is the one before
    'tail/2026103012/a', 'tail/2026103100/a', 'tail/2026103112/a', 'tail/2026110100/a',
    'thick/2026110112/a',
    'columns/2026103100/a', 'columns/2026110106/a', 'columns/2026110112/a', // not named: measured against tiles' run
  ];
  assert.deepEqual(backlogToDelete(paths, live), [
    'tiles/2026103100/a', 'tiles/2026110100/a',
    'tail/2026103012/a', 'tail/2026103100/a',
    'columns/2026103100/a',
  ]);
});

test('backlog: a manifest naming no tiles folder deletes nothing', () => {
  assert.throws(() => backlogToDelete(['tiles/a/x'], null), /no tiles folder/);
  assert.throws(() => backlogToDelete(['tiles/a/x'], { tiles: null }), /no tiles folder/);
});

test('summarize: files and bytes by family and by run', () => {
  const s = summarize([
    { pathname: 'tiles/A/x', size: 10 }, { pathname: 'tiles/A/y', size: 5 }, { pathname: 'tiles/B/x', size: 1 },
    { pathname: 'tail/A/x', size: 100 }, { pathname: 'manifest.json', size: 2 },
  ]);
  assert.deepEqual(s.total, { files: 5, bytes: 118 });
  assert.deepEqual(s.families, [['.', { files: 1, bytes: 2 }], ['tail', { files: 1, bytes: 100 }], ['tiles', { files: 3, bytes: 16 }]]);
  assert.deepEqual(s.runs, [['tail/A', { files: 1, bytes: 100 }], ['tiles/A', { files: 2, bytes: 15 }], ['tiles/B', { files: 1, bytes: 1 }]]);
});

test('fog folders are named by the manifest and kept for current and previous', () => {
  assert.deepEqual(manifestFolders({ fog: { tiles: 'fog/2026111012' } }), ['fog/2026111012/']);
  const c = { tiles: 'tiles/B', fog: { tiles: 'fog/B' } }, p = { tiles: 'tiles/A', fog: { tiles: 'fog/A' } };
  const paths = ['manifest.json', 'fog/B/N44W122/0.icc', 'fog/A/N44W122/0.icc', 'fog/Z/N44W122/0.icc'];
  assert.deepEqual(pathsToDelete(paths, { current: c, previous: p }), ['fog/Z/N44W122/0.icc']);
  assert.deepEqual(pathsToDelete(['fog/A/N44W122/0.icc'], { current: { tiles: 'tiles/B', fog: { tiles: 'fog/B' } }, previous: { fog: { tiles: 'fog/A' } } }), []);
});
