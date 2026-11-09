// publish/spread.test.mjs — a publish handles very many files. Spreading a list
// into a call overflows the stack at about 125,000 items.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('the publisher spreads no file list into a call', () => {
  for (const f of ['blob.mjs', 'roots.mjs', 'prune.mjs', 'sweep.mjs', 'store.mjs', 'prune-backlog.mjs']) {
    const src = readFileSync(new URL(f, import.meta.url), 'utf8');
    assert.doesNotMatch(src, /\.(push|concat|unshift)\(\s*\.\.\./, f);
    assert.doesNotMatch(src, /Math\.(max|min)\(\s*\.\.\./, f);
  }
});
