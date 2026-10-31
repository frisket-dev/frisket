import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createLaunchIntents, installLaunchDelivery, launchIntent } from '../src/launch-intent.mjs';

const tick = () => new Promise(resolve => setImmediate(resolve));

test('external intent is a strict guide URL, never an arbitrary app URL', () => {
  assert.equal(launchIntent('frisket://learn/court-filings?revision=1'), 'frisket://app/learn/court-filings?revision=1');
  for (const value of [
    'frisket://app/learn/court-filings?revision=1', 'frisket://user@learn/a?revision=1',
    'frisket://learn:1/a?revision=1', 'frisket://learn/a/../b?revision=1',
    'frisket://learn/%61?revision=1', 'frisket://learn/a%2fb?revision=1',
    'frisket://learn/a?revision=1&x=1', 'frisket://learn/a?revision=1&revision=2',
    'frisket://learn/a?revision=0', 'frisket://learn/a?revision=01',
    'frisket://learn/a?revision=9007199254740992', 'frisket://learn/a?revision=1#x',
    'frisket://learn/a/?revision=1', 'frisket://learn/--a?revision=1',
    'https://learn/a?revision=1', 'frisket://learn/a?revision=1\n',
  ]) assert.equal(launchIntent(value), null, value);
});

test('cold and warm deliveries wait for readiness and serialize with one pending intent', async () => {
  const seen = [];
  let finish;
  const intents = createLaunchIntents({ navigate: url => {
    seen.push(url);
    if (seen.length === 1) return new Promise(resolve => { finish = resolve; });
  } });
  intents.acceptArgs(['app.exe', 'frisket://app/private', 'frisket://learn/court-filings?revision=1']);
  assert.deepEqual(seen, []);
  intents.setReady(true);
  assert.deepEqual(seen, ['frisket://app/learn/court-filings?revision=1']);
  intents.accept('frisket://learn/first?revision=1');
  intents.accept('frisket://learn/second?revision=2');
  intents.setReady(false); // startup/recovery interrupts navigation
  finish();
  await tick();
  assert.equal(seen.length, 1);
  intents.setReady(true);
  await tick();
  assert.deepEqual(seen, ['frisket://app/learn/court-filings?revision=1', 'frisket://app/learn/second?revision=2']);
});

test('a failed navigation does not block later warm delivery', async () => {
  const seen = [];
  const intents = createLaunchIntents({ navigate: async url => { seen.push(url); if (seen.length === 1) throw new Error('cancelled'); } });
  intents.setReady(true);
  intents.accept('frisket://learn/first?revision=1');
  await tick();
  intents.accept('frisket://learn/second?revision=1');
  await tick();
  assert.equal(seen.length, 2);
});

test('Electron delivery fixture handles early macOS events and second-instance argv', async () => {
  class App extends EventEmitter {
    requestSingleInstanceLock() { return true; }
    quit() { throw new Error('unexpected quit'); }
  }
  const app = new App();
  const seen = [];
  let focuses = 0;
  const intents = createLaunchIntents({ navigate: async url => seen.push(url) });
  assert.equal(installLaunchDelivery({ app, intents, argv: ['app'], focus: () => focuses++ }), true);
  let prevented = false;
  app.emit('open-url', { preventDefault: () => { prevented = true; } }, 'frisket://learn/court-filings?revision=1');
  assert.equal(prevented, true);
  assert.deepEqual(seen, []);
  intents.setReady(true);
  await tick();
  app.emit('second-instance', {}, ['app', 'frisket://learn/council-recordings?revision=1']);
  await tick();
  assert.deepEqual(seen, ['frisket://app/learn/court-filings?revision=1', 'frisket://app/learn/council-recordings?revision=1']);
  assert.equal(focuses, 1);
});

test('recovery retries an interrupted navigation unless a newer intent replaced it', async () => {
  for (const newer of [false, true]) {
    const seen = [];
    let interrupt;
    const intents = createLaunchIntents({ navigate: url => {
      seen.push(url);
      if (seen.length === 1) return new Promise((_resolve, reject) => { interrupt = reject; });
    } });
    intents.setReady(true);
    intents.accept('frisket://learn/court-filings?revision=1');
    intents.setReady(false);
    if (newer) intents.accept('frisket://learn/council-recordings?revision=1');
    interrupt(new Error('navigation stopped for recovery'));
    await tick();
    assert.equal(seen.length, 1);
    intents.setReady(true);
    await tick();
    assert.deepEqual(seen, [
      'frisket://app/learn/court-filings?revision=1',
      `frisket://app/learn/${newer ? 'council-recordings' : 'court-filings'}?revision=1`,
    ]);
  }
});
