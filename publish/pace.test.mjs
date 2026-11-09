import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRetry, paceDelay, filesToSend } from './pace.mjs';

const clock = () => {
  let t = 1000;
  return { now: () => t, sleep: async (ms) => { t += ms; }, at: () => t };
};
const limited = (s) => Object.assign(new Error('Too many requests'), { retryAfter: s });

test('a rate limit waits the time named and uses up no attempt', async () => {
  const c = clock();
  const retry = createRetry({ attempts: 2, now: c.now, sleep: c.sleep });
  let calls = 0;
  const out = await retry('f', async () => { calls += 1; if (calls <= 5) throw limited(60); return 'ok'; });
  assert.equal(out, 'ok');
  assert.equal(calls, 6);
  assert.equal(c.at(), 1000 + 5 * 60000);
});
test('another worker also waits out a limit one of them hit', async () => {
  const c = clock();
  const retry = createRetry({ now: c.now, sleep: c.sleep });
  let first = true;
  await retry('a', async () => { if (first) { first = false; throw limited(60); } });
  const before = c.at();
  await retry('b', async () => {});
  assert.equal(c.at(), before);   // the pause is already over
  assert.equal(before, 1000 + 60000);
});
test('ordinary errors still fail after their attempts', async () => {
  const c = clock();
  const retry = createRetry({ attempts: 3, now: c.now, sleep: c.sleep });
  let calls = 0;
  await assert.rejects(retry('f', async () => { calls += 1; throw new Error('boom'); }), /f: boom/);
  assert.equal(calls, 3);
});
test('a limit that never lifts fails rather than hanging', async () => {
  const c = clock();
  const retry = createRetry({ now: c.now, sleep: c.sleep, maxRateWaits: 2 });
  await assert.rejects(retry('f', async () => { throw limited(60); }), /still rate limited/);
});
test('pacing', () => {
  assert.equal(paceDelay(0, 40, 0, 0), 0);
  assert.equal(paceDelay(40, 40, 0, 0), 1000);
  assert.equal(paceDelay(40, 40, 0, 5000), 0);
  assert.equal(paceDelay(40, 0, 0, 0), 0);
  assert.equal(paceDelay(40, undefined, 0, 0), 0);
});
test('a resumed publish sends only what is missing, and always the index', () => {
  const f = (path) => ({ path, dir: 'd' });
  const files = [f('tiles/v/a.tsv'), f('tiles/v/b.tsv'), f('tiles/v/index.json')];
  const have = ['tiles/v/a.tsv', 'tiles/v/index.json', 'manifest.json'];
  assert.deepEqual(filesToSend(files, have, true).map((x) => x.path), ['tiles/v/b.tsv', 'tiles/v/index.json']);
  assert.deepEqual(filesToSend(files, have, false), files);
});
