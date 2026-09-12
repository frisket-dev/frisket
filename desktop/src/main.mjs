import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { app, BrowserWindow, Menu, dialog, session, shell, protocol, net } from 'electron';
import { prepareRuntime } from './provision.mjs';
import { appUrl, installProtocol, APP_ORIGIN, APP_SCHEME } from './protocol.mjs';
import { startBackend } from './backend.mjs';
import { CleanupError } from './errors.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '..');
let mainWindow;
let backend;
let startupTask;
let provisioningController;
let quitting = false;
let recoveryTask;
let cleanupFailed = false;

// This must happen before Electron's ready event, before any renderer exists.
protocolPrivileges();
app.enableSandbox();

/** Register the single standard, secure application origin. */
export function protocolPrivileges() {
  protocol.registerSchemesAsPrivileged([{
    scheme: APP_SCHEME,
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      stream: true,
      codeCache: true,
      bypassCSP: false,
      allowServiceWorkers: false,
      corsEnabled: false,
    },
  }]);
}

/** @param {Electron.BrowserWindow} window */
export function protectWindow(window) {
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (appUrl(url)) {
      return { action: 'allow', overrideBrowserWindowOptions: { webPreferences: {
        sandbox: true, contextIsolation: true, nodeIntegration: false, webviewTag: false,
      } } };
    }
    openExternal(url);
    return { action: 'deny' };
  });
  window.webContents.on('did-create-window', (child) => protectWindow(child));
  window.webContents.on('will-navigate', (event, url) => {
    if (appUrl(url)) return;
    event.preventDefault();
    openExternal(url);
  });
}

/** @param {string} url */
export function openExternal(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') void shell.openExternal(parsed.href);
  } catch {}
}

function resourcesPath() {
  return app.isPackaged ? process.resourcesPath : path.join(desktopRoot, 'resources');
}

function createWindow() {
  const window = new BrowserWindow({
    width: 1240,
    height: 840,
    minWidth: 900,
    minHeight: 600,
    show: false,
    webPreferences: {
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
    },
  });
  protectWindow(window);
  window.once('ready-to-show', () => window.show());
  return window;
}

async function showStartup(window) {
  await window.loadFile(path.join(desktopRoot, 'ui', 'startup.html'));
  window.show();
}

/** @param {string} message */
function setStartupStatus(message) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.setTitle(`Frisket — ${message}`);
  void mainWindow.webContents.executeJavaScript(
    `document.querySelector('#status').textContent = ${JSON.stringify(message)}`,
  ).catch(() => {});
}

async function launch() {
  const dataPath = app.getPath('userData');
  const workspace = path.join(dataPath, 'workspace');
  await mkdir(workspace, { recursive: true });
  provisioningController = new AbortController();
  const runtime = await prepareRuntime({
    resourcesPath: resourcesPath(), dataPath, signal: provisioningController.signal, onProgress: setStartupStatus,
  });
  if (quitting) return;
  setStartupStatus('Opening your workspace…');
  backend = await startBackend({
    runtime, resourcesPath: resourcesPath(), workspace,
    onFailure: (error) => void recover(error),
  });
  if (quitting) {
    await backend.stop();
    return;
  }
  installProtocol({ protocol, net }, backend);
  await mainWindow.loadURL(`${APP_ORIGIN}/`);
}

async function recover(error) {
  if (recoveryTask) return recoveryTask;
  recoveryTask = (async () => {
    if (quitting) return;
    if (backend) {
      const old = backend;
      backend = undefined;
      try { await old.stop(); } catch (cleanup) {
        cleanupFailed = true;
        error = cleanup;
      }
    }
    cleanupFailed ||= error instanceof CleanupError;
    const choice = await dialog.showMessageBox(mainWindow, {
      type: 'error', buttons: cleanupFailed ? ['Quit'] : ['Retry', 'Quit'],
      defaultId: 0, cancelId: cleanupFailed ? 0 : 1,
      title: 'Frisket could not start',
      message: 'The local workspace service stopped unexpectedly.',
      detail: error instanceof Error ? error.message : 'Please try again.',
    });
    if (!cleanupFailed && choice.response === 0) setTimeout(() => { void startAttempt(); }, 0);
    else void requestQuit();
  })();
  try { await recoveryTask; } finally { recoveryTask = undefined; }
}

function startAttempt() {
  if (startupTask) return startupTask;
  startupTask = (async () => {
    try {
      await showStartup(mainWindow);
      await launch();
    } catch (error) {
      if (!quitting) await recover(error);
    } finally {
      startupTask = undefined;
      provisioningController = undefined;
    }
  })();
  return startupTask;
}

async function requestQuit() {
  if (quitting) return;
  quitting = true;
  provisioningController?.abort();
  const cleanup = Promise.allSettled([backend?.stop(), startupTask].filter(Boolean));
  let failed = cleanupFailed;
  try {
    const results = await Promise.race([
      cleanup,
      new Promise((_, reject) => setTimeout(() => reject(new Error('Desktop shutdown timed out.')), 12_000)),
    ]);
    failed ||= results.some((result) => result.status === 'rejected');
  } catch { failed = true; }
  if (failed) process.stderr.write('Frisket could not prove local service cleanup.\n');
  app.exit(failed ? 1 : 0);
}

function installMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { role: 'appMenu' },
    { label: 'File', submenu: [{ label: 'Open Data Folder', click: () => void shell.openPath(app.getPath('userData')) }, { type: 'separator' }, { label: 'Quit', accelerator: 'CmdOrCtrl+Q', click: () => void requestQuit() }] },
    { role: 'editMenu' }, { role: 'viewMenu' }, { role: 'windowMenu' },
  ]));
}

app.whenReady().then(async () => {
  if (!app.requestSingleInstanceLock()) { app.quit(); return; }
  // The existing UI copies text, but never needs clipboard reads or device access.
  const canWriteClipboard = (contents, permission, requestingUrl) =>
    permission === 'clipboard-sanitized-write' &&
    Boolean(contents && appUrl(contents.getURL()) && appUrl(requestingUrl));
  session.defaultSession.setPermissionRequestHandler((contents, permission, callback, details) =>
    callback(canWriteClipboard(contents, permission, details.requestingUrl)));
  session.defaultSession.setPermissionCheckHandler((contents, permission, requestingOrigin) =>
    canWriteClipboard(contents, permission, requestingOrigin));
  session.defaultSession.on('will-download', (_event, item, webContents) => {
    item.pause();
    void dialog.showSaveDialog(BrowserWindow.fromWebContents(webContents), {
      defaultPath: item.getFilename(),
    }).then(({ canceled, filePath }) => {
      if (canceled || !filePath) item.cancel();
      else { item.setSavePath(filePath); item.resume(); }
    });
  });
  app.on('second-instance', () => { mainWindow?.show(); mainWindow?.focus(); });
  installMenu();
  mainWindow = createWindow();
  await startAttempt();
});

app.on('window-all-closed', () => { if (process.platform !== 'darwin') void requestQuit(); });
app.on('activate', () => {
  if (!mainWindow || mainWindow.isDestroyed()) {
    mainWindow = createWindow();
    if (backend) void mainWindow.loadURL(`${APP_ORIGIN}/`);
    else void startAttempt();
  }
});
app.on('before-quit', (event) => {
  if (quitting) return;
  event.preventDefault();
  void requestQuit();
});
