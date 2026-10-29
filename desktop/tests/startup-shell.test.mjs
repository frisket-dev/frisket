import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

async function shell(windowsNewlines = false) {
  const windows = [];
  const shutdowns = [];
  class Window extends EventEmitter {
    constructor(options) {
      super(); this.options = options; this.bounds = { x: 10, y: 10, width: options.width, height: options.height };
      this.webContents = Object.assign(new EventEmitter(), { setWindowOpenHandler() {}, getURL: () => this.url, send() {} });
      windows.push(this);
    }
    async loadFile(file) { this.url = `file://${file}`; }
    async loadURL(url) { this.url = url; }
    show() { this.visible = true; }
    hide() { this.visible = false; }
    focus() {}
    center() {}
    setSize(width, height) { Object.assign(this.bounds, { width, height }); }
    setBounds(value) { this.bounds = value; }
    getBounds() { return this.bounds; }
    setOpacity(value) { this.opacity = value; }
    isDestroyed() { return Boolean(this.destroyed); }
    close() {
      let prevented = false;
      this.emit('close', { preventDefault() { prevented = true; } });
      if (!prevented) { this.destroyed = true; this.emit('closed'); }
    }
  }
  const app = Object.assign(new EventEmitter(), {
    getVersion: () => '1.0.0', getPath: () => '/tmp/profile', setPath() {},
    commandLine: { hasSwitch: () => true }, enableSandbox() {},
    whenReady: () => new Promise(() => {}),
  });
  let clock = 0;
  const context = vm.createContext({
    app, BrowserWindow: Window, process: { platform: 'linux', arch: 'x64' },
    path, fileURLToPath: () => '/app/src/main.mjs', pathToFileURL: value => ({ href: `file://${value}` }),
    mkdirSync() {}, mkdir: async () => {}, writeFile: async () => {},
    protocol: { registerSchemesAsPrivileged() {} },
    startupDiagnostics: () => ({ snapshot: () => ({}), record() {}, event() {} }),
    createUpdateProgress: () => ({ close() {} }), createBackendStopper: () => async () => {},
    shutdownDesktop: options => { shutdowns.push(options); },
    screen: { getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 1600, height: 1000 } }) },
    systemPreferences: { getAnimationSettings: () => ({ prefersReducedMotion: true }) },
    Date: { now: () => { clock += 150; return clock; } }, setTimeout: callback => callback(),
    appUrl: url => url.startsWith('frisket://app'), APP_SCHEME: 'frisket', APP_ORIGIN: 'frisket://app',
  });
  const input = await readFile(new URL('../src/main.mjs', import.meta.url), 'utf8');
  const source = (windowsNewlines ? input.replace(/\r?\n/g, '\r\n') : input)
    .replace(/^import .*;\r?\n/gm, '').replaceAll('export function', 'function').replace('import.meta.url', '"file:///app/src/main.mjs"');
  vm.runInContext(`${source}\nmainWindow = createWindow(); globalThis.proof = { showStartup, openStartupDetails, openHome: () => openHome(mainWindow) };`, context);
  return { windows, shutdowns, proof: context.proof };
}

test('startup card transitions to an ordinary framed workbench without quitting', async () => {
  const { windows, shutdowns, proof } = await shell();
  const home = windows[0];
  assert.equal(home.options.frame, undefined);
  assert.equal(home.options.webPreferences.preload, undefined);
  assert.equal(home.options.webPreferences.sandbox, true);
  await proof.showStartup();
  const card = windows[1];
  assert.equal(card.options.frame, false);
  assert.equal(card.options.width, 360);
  assert.match(card.options.webPreferences.preload, /startup-preload.cjs$/);
  assert.equal(home.visible, false);
  await proof.openHome();
  assert.equal(home.visible, true);
  assert.equal(home.url, 'frisket://app/');
  assert.equal(card.destroyed, true);
  assert.equal(shutdowns.length, 0);
});

test('closing startup requests guarded application shutdown instead of orphaning hidden Home', async () => {
  const { windows, shutdowns, proof } = await shell();
  await proof.showStartup();
  windows[1].close();
  assert.equal(shutdowns.length, 1);
  assert.equal(typeof shutdowns[0].abortStartup, 'function');
  assert.equal(typeof shutdowns[0].stopBackend, 'function');
});

test('opening details leaves window sizing to the rendered content', async () => {
  const { windows, proof } = await shell();
  await proof.showStartup();
  const card = windows[1];
  card.setSize(360, 267);
  await proof.openStartupDetails();
  assert.equal(card.getBounds().height, 267);
});

test('shell fixture accepts Windows checkout line endings', async () => {
  const { windows, shutdowns, proof } = await shell(true);
  await proof.showStartup();
  await proof.openHome();
  assert.equal(windows[0].url, 'frisket://app/');
  assert.equal(windows[1].destroyed, true);
  assert.equal(shutdowns.length, 0);
});
