import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pruneBacklog, reportLines } from './prune-backlog.mjs';
import { listAll, readManifest } from './store.mjs';

// A fake store: list pages of 2, del removes, fetch serves the manifest.
function fakeStore(files, manifest) {
  const store = new Map(files.map(([p, size]) => [p, size]));
  store.set('manifest.json', 50);
  const deleted = [];
  return {
    store, deleted,
    list: async ({ cursor, limit }) => {
      const all = [...store.keys()].sort();
      const from = cursor ? Number(cursor) : 0;
      const page = all.slice(from, from + Math.min(limit, 2));
      const next = from + page.length;
      return { blobs: page.map((p) => ({ pathname: p, size: store.get(p), url: `https://blob.test/${p}` })), hasMore: next < all.length, cursor: String(next) };
    },
    del: async (b) => { for (const p of b) { store.delete(p); deleted.push(p); } },
    fetch: async (url) => (url.startsWith('https://blob.test/manifest.json?') ? { ok: true, json: async () => manifest } : { ok: false }),
  };
}
const live = { run: '2026-11-01T12:00Z', tiles: 'tiles/2026110112', tail: { tiles: 'tail/2026110100' } };
const files = [
  ['tiles/2026103100/a', 1e9], ['tiles/2026110100/a', 1e9], ['tiles/2026110106/a', 2e6], ['tiles/2026110112/a', 3e6],
  ['tail/2026103100/a', 5e8], ['tail/2026103112/a', 1e6], ['tail/2026110100/a', 1e6],
];
const direct = (w, f) => f();

test('listAll pages through the store; readManifest reads the live manifest', async () => {
  const s = fakeStore(files, live);
  const blobs = await listAll(s.list, direct);
  assert.equal(blobs.length, 8);
  assert.deepEqual(await readManifest(blobs, s.fetch), live);
  assert.equal(await readManifest(blobs.filter((b) => b.pathname !== 'manifest.json'), s.fetch), null);
  assert.equal(await readManifest(blobs, async () => { throw new Error('offline'); }), null);
});

test('a dry run reports per folder and per run and deletes nothing', async () => {
  const s = fakeStore(files, live);
  const logs = [];
  const r = await pruneBacklog({ list: s.list, del: s.del, fetch: s.fetch, retry: direct, dryRun: true, log: (l) => logs.push(l) });
  assert.equal(s.deleted.length, 0);
  assert.equal(r.deleted, 0);
  assert.deepEqual(r.doomed.map((b) => b.pathname), ['tail/2026103100/a', 'tiles/2026103100/a', 'tiles/2026110100/a']);
  assert.ok(logs.includes('would delete: 3 files, 2.50 GB'));
  assert.ok(logs.includes('  tiles/  2 files, 2.00 GB'));
  assert.ok(logs.includes('    tail/2026103100/  1 files, 500.0 MB'));
  assert.ok(logs.includes('dry run: nothing deleted'));
});

test('a real run deletes the old runs, paced, and keeps the live one, the one before and manifest.json', async () => {
  const s = fakeStore(files, live);
  const logs = [];
  const r = await pruneBacklog({ list: s.list, del: s.del, fetch: s.fetch, retry: direct, dryRun: false, perSec: 0, log: (l) => logs.push(l) });
  assert.equal(r.deleted, 3);
  assert.deepEqual([...s.store.keys()].sort(), ['manifest.json', 'tail/2026103112/a', 'tail/2026110100/a', 'tiles/2026110106/a', 'tiles/2026110112/a']);
  assert.ok(logs.some((l) => /^deleted 3 files, 2\.50 GB in \d+ s$/.test(l)));
});

test('no readable manifest: refuses and deletes nothing', async () => {
  const s = fakeStore(files, live);
  s.fetch = async () => ({ ok: false });
  await assert.rejects(pruneBacklog({ list: s.list, del: s.del, fetch: s.fetch, retry: direct, dryRun: false, log: () => {} }), /refusing/);
  assert.equal(s.deleted.length, 0);
});

test('reportLines', () => {
  assert.deepEqual(reportLines('keep', []), ['keep: 0 files, 0.0 MB']);
});
