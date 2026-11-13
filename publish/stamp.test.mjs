import { test } from 'node:test';
import assert from 'node:assert/strict';
import { stampManifest } from './stamp.mjs';

const read = (b) => JSON.parse(b.toString());

test('a fresh manifest gets publishedMs, now plus the cache time; every other key as it was', () => {
  const src = Buffer.from('{"run":"2026-11-12T18:00:00Z","runMs":1762970400000,"builtMs":1762984000000,"squares":["N44W122"],"tail":null}');
  const out = read(stampManifest(src, 1762986000123, 60000));
  assert.deepEqual(out, { ...read(src), publishedMs: 1762986060123 });
  assert.equal(read(stampManifest(src, 1000)).publishedMs, 1000);          // no cache given
});

test('a copy of a live manifest (the HRRR fog add-on) keeps the run\'s time and gets republishedMs', () => {
  const live = stampManifest(Buffer.from('{"runMs":1}'), 1000, 60000);
  const fog = { ...read(live), fog: { run: 'r' } };
  const out = read(stampManifest(Buffer.from(JSON.stringify(fog)), 5000, 60000));
  assert.equal(out.publishedMs, 61000);
  assert.equal(out.republishedMs, 65000);
  assert.equal(read(stampManifest(Buffer.from(JSON.stringify(out)), 9000)).republishedMs, 9000);
});

test('an unusable publishedMs is stamped over, as a fresh manifest', () => {
  for (const bad of [null, '5', 0, -1]) {
    const out = read(stampManifest(Buffer.from(JSON.stringify({ publishedMs: bad })), 7000));
    assert.equal(out.publishedMs, 7000, String(bad));
    assert.equal(out.republishedMs, undefined);
  }
});

test('refuses a manifest that is not an object, or a bad time', () => {
  assert.throws(() => stampManifest(Buffer.from('[1]'), 1000));
  assert.throws(() => stampManifest(Buffer.from('{not json'), 1000));
  assert.throws(() => stampManifest(Buffer.from('{}'), NaN));
  assert.throws(() => stampManifest(Buffer.from('{}'), 1000, -1));
});
