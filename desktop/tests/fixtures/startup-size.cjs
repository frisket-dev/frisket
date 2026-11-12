const assert = require('node:assert/strict');
const path = require('node:path');
const { app, BrowserWindow, ipcMain } = require('electron');

app.whenReady().then(async () => {
  const { installStartupBridge, resizeStartupWindow } = await import('../../src/startup-bridge.mjs');
  const page = path.resolve(__dirname, '../../ui/startup.html');
  const pageUrl = require('node:url').pathToFileURL(page).href;
  const window = new BrowserWindow({
    width: 360, height: 390, frame: false, resizable: false, show: false,
    webPreferences: { preload: path.resolve(__dirname, '../../src/startup-preload.cjs'), sandbox: true, contextIsolation: true, nodeIntegration: false },
  });
  // A fixed native window may retain the construction-time size constraints.
  window.setMinimumSize(360, 390);
  window.setMaximumSize(360, 390);
  let state = { status: 'checking', steps: [], logs: [], detailsOpen: false };
  installStartupBridge({
    ipcMain, pageUrl, findWindow: contents => contents === window.webContents ? window : undefined,
    snapshot: () => state, retry() {}, close() {}, copy() {},
    details: (_window, open) => { state.detailsOpen = open; },
    resize: resizeStartupWindow,
  });
  await window.loadFile(page);
  window.show();
  async function fitted(label) {
    // Native resize is asynchronous on some platforms. Compare the actual client
    // bounds and viewport with the live card, not a mocked setSize invocation.
    let measured;
    for (let attempt = 0; attempt < 100; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 20));
      measured = await window.webContents.executeJavaScript(`({
        card: document.querySelector('.startup-card').getBoundingClientRect().height,
        viewport: innerHeight, width: innerWidth,
      })`);
      const zoom = window.webContents.getZoomFactor();
      if (window.getContentSize()[1] === Math.ceil(Math.ceil(measured.card) * zoom)
        && Math.abs(measured.viewport - measured.card) <= 2 / zoom) break;
    }
    const zoom = window.webContents.getZoomFactor();
    assert.deepEqual(window.getContentSize(), [Math.ceil(360 * zoom), Math.ceil(Math.ceil(measured.card) * zoom)], label);
    assert.ok(Math.abs(measured.viewport - measured.card) <= 2 / zoom, label);
    assert.ok(Math.abs(measured.width - 360) <= 1 / zoom, label);
    if (process.platform !== 'linux') assert.equal(window.isResizable(), false);
    return Math.ceil(measured.card);
  }
  const collapsed = await fitted('initial content fits');
  await window.webContents.executeJavaScript("document.getElementById('details-toggle').click()");
  assert.ok(await fitted('details expand') > collapsed);
  await window.webContents.executeJavaScript("document.getElementById('details-toggle').click()");
  assert.equal(await fitted('details collapse'), collapsed);
  state.firstTime = true;
  window.webContents.send('startup:state', state);
  assert.ok(await fitted('first-install notice expands') > collapsed);
  state = { ...state, status: 'failed', retryable: true, failure: { cause: 'unknown' }, detailsOpen: true };
  window.webContents.send('startup:state', state);
  await fitted('failure and details fit');
  state = { ...state, status: 'checking', failure: null, firstTime: false, detailsOpen: false };
  window.webContents.send('startup:state', state);
  assert.equal(await fitted('retry contracts'), collapsed);
  state = { ...state, status: 'done', detailsOnly: true, detailsOpen: true };
  window.webContents.send('startup:state', state);
  await fitted('standalone diagnostics fit');
  for (const zoom of [0.5, 0.75, 1.25, 1]) {
    window.webContents.setZoomFactor(zoom);
    await fitted(`diagnostics fit at ${zoom} zoom`);
    state = { ...state, status: 'checking', detailsOnly: false, detailsOpen: false };
    window.webContents.send('startup:state', state);
    await fitted(`collapsed content fits at ${zoom} zoom`);
    await window.webContents.executeJavaScript("document.getElementById('details-toggle').click()");
    await fitted(`details expand at ${zoom} zoom`);
  }
  app.exit(0);
}).catch(error => { console.error(error); app.exit(1); });
