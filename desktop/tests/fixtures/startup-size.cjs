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
        card: Math.ceil(document.querySelector('.startup-card').getBoundingClientRect().height),
        viewport: innerHeight,
      })`);
      if (window.getContentSize()[1] === measured.card && measured.viewport === measured.card) break;
    }
    assert.deepEqual(window.getContentSize(), [360, measured.card], label);
    assert.equal(measured.viewport, measured.card, label);
    if (process.platform !== 'linux') assert.equal(window.isResizable(), false);
    return measured.card;
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
  app.exit(0);
}).catch(error => { console.error(error); app.exit(1); });
