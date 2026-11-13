import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { planTexdev, publishTexdev, PREFIX, MANIFEST } from './texdevplan.mjs';

const NEW = ['texdev/manifest.json', 'texdev/upper/2026111212/N40W125.icu', 'texdev/clues/2026111212/N40W125.icx'];
const OLD = ['texdev/manifest.json', 'texdev/upper/2026111206/N40W125.icu', 'texdev/clues/2026111206/N40W125.icx'];

async function outDir(paths) {
  const d = await mkdtemp(join(tmpdir(), 'texdev-'));
  for (const p of paths) {
    await mkdir(dirname(join(d, p)), { recursive: true });
    await writeFile(join(d, p), p);
  }
  return d;
}

// A fake Blob store that records every call in order.
function store(initial) {
  const have = new Set(initial);
  const calls = [];
  return {
    calls, have,
    put: async (p, body, opts) => { calls.push(['put', p, opts]); have.add(p); },
    del: async (batch) => { for (const p of batch) { calls.push(['del', p]); have.delete(p); } },
    list: async ({ prefix, cursor }) => {
      calls.push(['list', prefix, cursor]);
      return { blobs: [...have].filter((p) => p.startsWith(prefix)).map((pathname) => ({ pathname })), hasMore: false };
    },
  };
}

const retry = async (what, fn) => fn();

test('the plan: uploads the new run, the manifest last, then deletes only the previous texdev run', () => {
  const plan = planTexdev(NEW, OLD);
  assert.deepEqual(plan.uploads, NEW.filter((p) => p !== MANIFEST));
  assert.equal(plan.manifest, MANIFEST);
  assert.deepEqual(plan.deletes, OLD.filter((p) => p !== MANIFEST));
});

test('the plan refuses any local path outside texdev/', () => {
  for (const bad of ['manifest.json', 'tiles/2026111212/N40W125.icl', 'columns/x.icc', 'texdev2/a', 'texdev/../manifest.json', '/texdev/a']) {
    assert.throws(() => planTexdev([...NEW, bad], []), /outside texdev\//, bad);
  }
});

test('the plan refuses a listing that reaches outside texdev/', () => {
  assert.throws(() => planTexdev(NEW, ['manifest.json']), /outside texdev\//);
  assert.throws(() => planTexdev(NEW, ['tiles/2026111206/N40W125.icl']), /outside texdev\//);
});

test('the plan needs a texdev manifest', () => {
  assert.throws(() => planTexdev(NEW.filter((p) => p !== MANIFEST), []), /texdev\/manifest\.json/);
});

test('a re-run of the same run deletes nothing', () => {
  assert.deepEqual(planTexdev(NEW, NEW).deletes, []);
});

test('publish: every put and delete is under texdev/, the deletes come after the manifest, the live feed is untouched', async () => {
  const live = ['manifest.json', 'tiles/2026111206/N40W125.icl', 'columns/2026111206/N40W125.icc'];
  const s = store([...live, ...OLD]);
  const r = await publishTexdev({ outDir: await outDir(NEW), put: s.put, list: s.list, del: s.del, retry, log: () => {} });
  assert.equal(r.ok, true);
  const puts = s.calls.filter((c) => c[0] === 'put').map((c) => c[1]);
  const dels = s.calls.filter((c) => c[0] === 'del').map((c) => c[1]);
  assert.ok([...puts, ...dels].every((p) => p.startsWith(PREFIX)));
  assert.deepEqual(new Set(puts), new Set(NEW));
  assert.equal(puts.at(-1), MANIFEST);
  const manifestAt = s.calls.findIndex((c) => c[0] === 'put' && c[1] === MANIFEST);
  assert.ok(s.calls.every((c, i) => c[0] !== 'del' || i > manifestAt));
  assert.deepEqual(dels.sort(), OLD.filter((p) => p !== MANIFEST).sort());
  // Every listing asked for texdev/ only.
  assert.ok(s.calls.filter((c) => c[0] === 'list').every((c) => c[1] === PREFIX));
  for (const p of live) assert.ok(s.have.has(p), p);
  // The manifest is cached briefly, the run's files for a year (as blob.mjs).
  const opts = Object.fromEntries(s.calls.filter((c) => c[0] === 'put').map((c) => [c[1], c[2]]));
  assert.equal(opts[MANIFEST].cacheControlMaxAge, 60);
  assert.equal(opts[NEW[1]].cacheControlMaxAge, 365 * 24 * 3600);
});

test('publish: a stray file in the out dir stops it before anything is uploaded', async () => {
  const s = store(OLD);
  await assert.rejects(publishTexdev({ outDir: await outDir([...NEW, 'manifest.json']), put: s.put, list: s.list, del: s.del, retry, log: () => {} }),
    /outside texdev\//);
  assert.equal(s.calls.filter((c) => c[0] !== 'list').length, 0);
});

test('publish: a failed delete leaves the new manifest live and reports, never throws', async () => {
  const s = store(OLD);
  const logs = [];
  const r = await publishTexdev({ outDir: await outDir(NEW), put: s.put, list: s.list,
    del: async () => { throw new Error('boom'); }, retry, log: (l) => logs.push(l) });
  assert.equal(r.ok, false);
  assert.ok(s.have.has(MANIFEST));
  assert.ok(logs.some((l) => l.startsWith('::warning::')));
});
