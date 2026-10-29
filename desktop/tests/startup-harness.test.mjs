import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { EventEmitter } from 'node:events';
import { waitForStartup, observeWindows } from '../e2e/startup.mjs';

function page(url, state) {
  let retries = 0;
  let unsubscribed = false;
  const context = vm.createContext({ window: {
    frisketStartup: {
      subscribe(callback) { callback(state); return () => { unsubscribed = true; }; },
      retry() { retries += 1; },
    },
  } });
  return {
    url: () => url, isClosed: () => false,
    evaluate: async callback => vm.runInContext(`(${callback.toString()})()`, context),
    retries: () => retries, unsubscribed: () => unsubscribed,
  };
}

test('in-card failure aborts promptly with saved diagnostics and never retries', async t => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'startup-harness-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const diagnosticsPath = path.join(root, 'startup-diagnostics.txt');
  await fs.writeFile(diagnosticsPath, 'Frisket test diagnostics\nDownload host unreachable');
  for (const status of ['failed', 'stopped']) {
    const card = page('file:///app/ui/startup.html', { status, failure: { cause: 'network' } });
    await assert.rejects(waitForStartup({ windows: () => [card] }, { timeout: 60_000, diagnosticsPath }), error => {
      assert.match(error.message, /startup failed \(no retry\): network/);
      assert.match(error.message, /Download host unreachable/);
      assert.equal(error.startupPage, card);
      return true;
    });
    assert.equal(card.retries(), 0);
    assert.equal(card.unsubscribed(), true);
  }
});

test('card closes and a separate Home window is returned', async () => {
  const card = page('file:///app/ui/startup.html', { status: 'running' });
  const home = page('frisket://app/', {});
  let reads = 0;
  const electron = { windows: () => ++reads === 1 ? [card] : [home] };
  assert.equal(await waitForStartup(electron, { timeout: 1000, pollMs: 0 }), home);
  assert.equal(card.unsubscribed(), true);
  assert.equal(card.retries(), 0);
});

test('already-open Home needs no card or startup navigation', async () => {
  const home = page('frisket://app/p/project', {});
  assert.equal(await waitForStartup({ windows: () => [home] }), home);
});

test('failure snapshot supplies diagnostics before the saved file exists', async () => {
  const card = page('file:///app/ui/startup.html', {
    status: 'stopped', failure: { cause: 'disk_space' }, logs: [{ message: 'Disk full' }],
  });
  await assert.rejects(waitForStartup({ windows: () => [card] }, { diagnosticsPath: '/missing/diagnostics' }), /Disk full/);
});

test('observers cover existing and new Home windows once, including early and later errors', () => {
  const electron = new EventEmitter();
  const card = new EventEmitter();
  const home = new EventEmitter();
  electron.windows = () => [card, card];
  const errors = [];
  const attached = [];
  const detach = observeWindows(electron, page => {
    attached.push(page);
    page.on('pageerror', error => errors.push(error.message));
  });
  card.emit('pageerror', new Error('card error'));
  electron.emit('window', home);
  home.emit('pageerror', new Error('early Home error'));
  electron.emit('window', home);
  home.emit('pageerror', new Error('later Home error'));
  assert.deepEqual(attached, [card, home]);
  assert.deepEqual(errors, ['card error', 'early Home error', 'later Home error']);
  detach();
  assert.equal(electron.listenerCount('window'), 0);
});
