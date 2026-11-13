import { test } from 'node:test';
import assert from 'node:assert/strict';
import { stampManifest } from './stamp.mjs';

test('adds publishedMs and keeps every other key as it was', () => {
  const src = Buffer.from('{"run":"2026-11-12T18:00:00Z","runMs":1762970400000,"builtMs":1762984000000,"squares":["N44W122"],"tail":null}');
  const out = JSON.parse(stampManifest(src, 1762986000123).toString());
  assert.deepEqual(out, { ...JSON.parse(src.toString()), publishedMs: 1762986000123 });
});

test('a re-stamp replaces the earlier time', () => {
  const once = stampManifest(Buffer.from('{"runMs":1}'), 1000);
  assert.equal(JSON.parse(stampManifest(once, 2000).toString()).publishedMs, 2000);
});

test('refuses a manifest that is not an object, or a bad time', () => {
  assert.throws(() => stampManifest(Buffer.from('[1]'), 1000));
  assert.throws(() => stampManifest(Buffer.from('{not json'), 1000));
  assert.throws(() => stampManifest(Buffer.from('{}'), NaN));
});
