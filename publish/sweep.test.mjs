import { test } from 'node:test';
import assert from 'node:assert/strict';
import { deletePaced } from './sweep.mjs';
import { createRetry } from './pace.mjs';

const clock = () => {
  let t = 1000;
  return { now: () => t, sleep: async (ms) => { t += ms; }, at: () => t };
};
const paths = (n) => Array.from({ length: n }, (_, i) => `tiles/old/${i}.icl`);
const bare429 = () => Object.assign(new Error('Vercel Blob: Too many requests please lower the number of concurrent requests .'), { retryAfter: 0 });

test('deletes in batches of 100 at the paced rate, logs progress and returns the count', async () => {
  const c = clock();
  const calls = [];
  const logs = [];
  const n = await deletePaced(paths(1050), {
    del: async (b) => { calls.push({ at: c.at(), n: b.length }); },
    retry: createRetry({ now: c.now, sleep: c.sleep }),
    perSec: 100, now: c.now, sleep: c.sleep, log: (l) => logs.push(l), every: 500,
  });
  assert.equal(n, 1050);
  assert.equal(calls.length, 11);
  assert.ok(calls.every((x, i) => x.n === (i < 10 ? 100 : 50)));
  // One batch of 100 a second at 100 deletes a second.
  assert.deepEqual(calls.map((x) => x.at - 1000), [0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000]);
  assert.deepEqual(logs, ['deleted 500 of 1050 files…', 'deleted 1000 of 1050 files…']);
});

test('a bare "Too many requests" waits 60 s and goes on, without bursting to catch up', async () => {
  const c = clock();
  const calls = [];
  let hit = false;
  const n = await deletePaced(paths(400), {
    del: async (b) => {
      calls.push(c.at() - 1000);
      if (calls.length === 2 && !hit) { hit = true; throw bare429(); }
    },
    retry: createRetry({ attempts: 2, now: c.now, sleep: c.sleep }),
    perSec: 100, now: c.now, sleep: c.sleep, log: () => {},
  });
  assert.equal(n, 400);
  // batch 2 is refused at 1 s, retried at 61 s; batches 3 and 4 follow a second apart.
  assert.deepEqual(calls, [0, 1000, 61000, 62000, 63000]);
});

test('a failure reports how many went before it', async () => {
  const c = clock();
  let k = 0;
  const err = await deletePaced(paths(300), {
    del: async () => { k += 1; if (k === 3) throw new Error('store suspended'); },
    retry: createRetry({ attempts: 1, now: c.now, sleep: c.sleep }),
    perSec: 0, now: c.now, sleep: c.sleep, log: () => {},
  }).catch((e) => e);
  assert.match(err.message, /delete: store suspended/);
  assert.equal(err.deleted, 200);
});

test('nothing to delete makes no call', async () => {
  let calls = 0;
  assert.equal(await deletePaced([], { del: async () => { calls += 1; }, retry: (w, f) => f() }), 0);
  assert.equal(calls, 0);
});

import { pruneOldRuns } from './sweep.mjs';
import { pathsToDelete } from './prune.mjs';

const m = (r) => ({ run: r, tiles: `tiles/${r}` });
const listing = () => ['manifest.json', ...['A', 'B', 'C', 'D', 'E'].flatMap((r) => [0, 1, 2].map((k) => ({ r, k })))
  .map(({ r, k }) => `tiles/${r}/${k}.icl`)].map((pathname) => ({ pathname }));

test('the publish prune clears a backlog of runs, not only the one the previous manifest replaced', async () => {
  const gone = [];
  const logs = [];
  const r = await pruneOldRuns({
    listAll: async () => listing(), pathsToDelete, current: m('E'), previous: m('D'),
    del: async (b) => { gone.push(...b); }, retry: (w, f) => f(), perSec: 0, log: (l) => logs.push(l),
  });
  assert.equal(r.ok, true);
  assert.equal(r.deleted, 9);
  assert.deepEqual(gone, ['A', 'B', 'C'].flatMap((x) => [0, 1, 2].map((k) => `tiles/${x}/${k}.icl`)));
  assert.match(logs.at(-1), /^deleted 9 files from older runs in \d+ s$/);
});

test('a prune that fails warns and returns; it never throws (the publish already worked)', async () => {
  const c = clock();
  const logs = [];
  let calls = 0;
  const r = await pruneOldRuns({
    listAll: async () => listing(), pathsToDelete, current: m('E'), previous: m('D'),
    del: async () => { calls += 1; if (calls > 1) throw bare429(); },
    retry: createRetry({ now: c.now, sleep: c.sleep, maxRateWaits: 2 }), perSec: 0,
    log: (l) => logs.push(l), sweep: { batch: 4, now: c.now, sleep: c.sleep },
  });
  assert.equal(r.ok, false);
  assert.equal(r.deleted, 4);
  assert.equal(logs.length, 1);
  assert.match(logs[0], /^::warning::blob publish: run E is live, but pruning older runs stopped after 4 of 9 files \(delete: still rate limited/);
});

test('a listing that fails is a warning too', async () => {
  const logs = [];
  const r = await pruneOldRuns({
    listAll: async () => { throw new Error('list: boom'); }, pathsToDelete, current: m('E'), previous: null,
    del: async () => {}, retry: (w, f) => f(), perSec: 0, log: (l) => logs.push(l),
  });
  assert.equal(r.ok, false);
  assert.match(logs[0], /^::warning::.*stopped after 0 of 0 files \(list: boom\)/);
});
