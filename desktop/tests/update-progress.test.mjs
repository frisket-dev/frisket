import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import test from 'node:test';
import { createUpdateProgress } from '../src/update-progress.mjs';

function harness() {
  const windows = [];
  const parent = { isDestroyed: () => false };
  class Window extends EventEmitter {
    destroyed = false;
    scripts = [];
    webContents = Object.assign(new EventEmitter(), {
      executeJavaScript: async (script) => { this.scripts.push(script); },
      setWindowOpenHandler: (handler) => { this.openHandler = handler; },
    });
    constructor(options) { super(); this.options = options; windows.push(this); }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; }
    setMenu(menu) { this.menu = menu; }
    loadFile(file) { this.file = file; return new Promise((resolve) => { this.finishLoad = resolve; }); }
  }
  return {
    windows, parent,
    progress: createUpdateProgress({ BrowserWindow: Window, getParent: () => parent, pagePath: '/update.html' }),
  };
}

test('download blocks the workspace and window close, and renders latest progress after loading', async () => {
  const { progress, windows, parent } = harness();
  progress.update({ phase: 'downloading' });
  const window = windows[0];
  assert.equal(window.options.parent, parent);
  assert.equal(window.options.modal, true);
  assert.equal(window.options.show, true);
  assert.equal(window.options.closable, false);
  assert.equal(window.options.webPreferences.nodeIntegration, false);
  assert.equal(window.options.webPreferences.sandbox, true);
  assert.deepEqual(window.openHandler(), { action: 'deny' });
  let prevented = false;
  window.emit('close', { preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  progress.update({ phase: 'downloading', percent: 47.5 });
  assert.equal(windows.length, 1);
  assert.equal(window.scripts.length, 0);
  window.finishLoad();
  await new Promise(setImmediate);
  assert.match(window.scripts.at(-1), /"percent":47.5/);
  progress.update({ phase: 'downloading', percent: 120 });
  assert.match(window.scripts.at(-1), /"percent":100/);
  progress.update({ phase: 'ready' });
  assert.equal(window.destroyed, true);
});

test('failure and shutdown release the modal, even when download finished before the UI loaded', async () => {
  for (const phase of ['ready', 'error']) {
    const { progress, windows } = harness();
    progress.update({ phase: 'downloading', percent: 20 });
    progress.update({ phase });
    windows[0].finishLoad();
    await new Promise(setImmediate);
    assert.equal(windows[0].destroyed, true);
    assert.equal(windows[0].scripts.length, 0);
    progress.update({ phase: 'downloading' });
    progress.close();
    assert.equal(windows[1].destroyed, true);
  }
});

test('progress renderer shows percentage, fills the bar and distinguishes final verification', async () => {
  const elements = { progress: {}, percent: {}, status: {} };
  const context = { window: {}, document: { getElementById: (id) => elements[id] } };
  vm.runInNewContext(await readFile(new URL('../ui/update.js', import.meta.url), 'utf8'), context);
  context.window.updateDownloadProgress({ percent: 47.5 });
  assert.equal(elements.progress.value, 47.5);
  assert.equal(elements.percent.textContent, '47%');
  assert.equal(elements.status.textContent, 'Downloading…');
  context.window.updateDownloadProgress({ percent: 100 });
  assert.equal(elements.percent.textContent, '100%');
  assert.equal(elements.status.textContent, 'Finishing download…');
});
