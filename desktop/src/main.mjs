import { mkdir } from 'node:fs/promises';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { app, BrowserWindow, Menu, Notification, dialog, session, shell, protocol } from 'electron';
import electronUpdater from 'electron-updater';
import { prepareRuntime } from './provision.mjs';
import { appUrl, installProtocol, APP_ORIGIN, APP_SCHEME } from './protocol.mjs';
import { startBackend } from './backend.mjs';
import { CleanupError } from './errors.mjs';
import { createUpdater } from './updater.mjs';
import { createBackendStopper, shutdownDesktop } from './shutdown.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '..');
if (process.platform === 'win32') app.setAppUserModelId('dev.frisket.desktop');
let mainWindow;
let backend;
let startupTask;
let provisioningController;
let quitting = false;
let recoveryTask;
let cleanupFailed = false;
let updates;
const stopBackend = createBackendStopper(() => {
  const current = backend;
  backend = undefined;
  return current;
});

// Keep the original beta's projects, caches and browser preferences across the rename.
if (!app.commandLine.hasSwitch('user-data-dir')) {
  const dataPath = path.join(app.getPath('appData'), 'Frisket');
  mkdirSync(dataPath, { recursive: true });
  app.setPath('userData', dataPath);
}
mkdirSync(app.getPath('userData'), { recursive: true });
app.setPath('sessionData', app.getPath('userData'));

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
    title: 'Frisket Desktop',
    width: 1240,
    height: 840,
    minWidth: 900,
    minHeight: 600,
    show: false,
    backgroundColor: '#f9f7fc',
    webPreferences: {
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
    },
  });
  protectWindow(window);
  window.on('page-title-updated', (event) => event.preventDefault());
  window.once('ready-to-show', () => window.show());
  window.webContents.on('render-process-gone', (_event, details) => {
    if (!quitting) void recover(new Error(`The workspace window stopped (${details.reason}).`));
  });
  return window;
}

async function showStartup(window) {
  await window.loadFile(path.join(desktopRoot, 'ui', 'startup.html'));
  window.show();
}

/** @param {{phase: string, message: string}} progress */
function setStartupStatus(progress) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  void mainWindow.webContents.executeJavaScript(
    `window.updateStartup(${JSON.stringify(progress)})`,
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
  setStartupStatus({ phase: 'workspace', message: 'Opening your workspace…' });
  backend = await startBackend({
    runtime, resourcesPath: resourcesPath(), workspace,
    onFailure: (error) => void recover(error),
  });
  if (quitting) {
    await stopBackend();
    return;
  }
  installProtocol({ protocol }, backend);
  setStartupStatus({ phase: 'ready', message: 'Your workspace is ready.' });
  await mainWindow.loadURL(`${APP_ORIGIN}/`);
}

async function recover(error) {
  if (recoveryTask) return recoveryTask;
  recoveryTask = (async () => {
    if (quitting) return;
    if (backend) {
      try { await stopBackend(); } catch (cleanup) {
        cleanupFailed = true;
        error = cleanup;
      }
    }
    cleanupFailed ||= error instanceof CleanupError;
    const choice = await dialog.showMessageBox(mainWindow, {
      type: 'error', buttons: cleanupFailed ? ['Quit'] : ['Retry', 'Quit'],
      defaultId: 0, cancelId: cleanupFailed ? 0 : 1,
      title: 'Frisket Desktop encountered a problem',
      message: 'Frisket Desktop could not continue.',
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
      if (!quitting) await launch();
    } catch (error) {
      if (quitting && error instanceof CleanupError) throw error;
      if (!quitting) await recover(error);
    } finally {
      startupTask = undefined;
      provisioningController = undefined;
    }
  })();
  return startupTask;
}

async function requestQuit(installUpdate = false) {
  if (quitting) return;
  quitting = true;
  return shutdownDesktop({
    abortStartup: () => provisioningController?.abort(),
    stopBackend,
    startupTask,
    cleanupFailed,
    fail: async () => {
      process.stderr.write('Frisket could not prove local service cleanup.\n');
      if (installUpdate) await dialog.showMessageBox({
        type: 'error', title: 'Update not installed', message: 'Frisket could not finish stopping its local services.',
        detail: 'Reopen Frisket and try the update again.', buttons: ['OK'],
      });
      app.exit(1);
    },
    finish: () => {
      if (!installUpdate) { app.exit(0); return; }
      // The updater owns the final quit/relaunch; our before-quit handler must
      // let it proceed now that Python and its workers have stopped.
      electronUpdater.autoUpdater.once('error', async () => {
        await dialog.showMessageBox({
          type: 'error', title: 'Update not installed', message: 'Frisket could not install the update.',
          detail: 'Reopen Frisket and try again, or download the latest installer from frisket.dev.', buttons: ['OK'],
        });
        app.exit(1);
      });
      electronUpdater.autoUpdater.quitAndInstall(false, true);
    },
  });
}

function installMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { role: 'appMenu' },
    { label: 'File', submenu: [{ label: 'Open Data Folder', click: () => void shell.openPath(app.getPath('userData')) }, { type: 'separator' }, { label: 'Quit', accelerator: 'CmdOrCtrl+Q', click: () => void requestQuit() }] },
    { role: 'editMenu' }, { role: 'viewMenu' }, { role: 'windowMenu' },
    { role: 'help', submenu: [{
      id: 'desktop-update', label: 'Check for Updates…', enabled: Boolean(updates),
      click: () => void updates?.check({ manual: true }),
    }] },
  ]));
}

function setupUpdates() {
  if (!app.isPackaged || !['darwin', 'win32'].includes(process.platform)) return;
  updates = createUpdater({
    updater: electronUpdater.autoUpdater,
    requestInstall: () => requestQuit(true),
    message: async (options) => (await dialog.showMessageBox({
      ...options, defaultId: options.buttons.length - 1, cancelId: options.buttons.length - 1,
    })).response,
    notify: (title, body, onClick) => {
      if (!Notification.isSupported()) return;
      const notification = new Notification({ title, body });
      notification.once('click', onClick);
      notification.show();
    },
    onState: ({ phase, percent }) => {
      const item = Menu.getApplicationMenu()?.getMenuItemById('desktop-update');
      if (!item) return;
      item.label = phase === 'ready' ? 'Restart to update…'
        : phase === 'checking' ? 'Checking for updates…'
          : phase === 'downloading' ? `Downloading update${Number.isFinite(percent) ? ` (${Math.floor(percent)}%)` : ''}…`
            : 'Check for Updates…';
      item.enabled = !quitting && !['checking', 'downloading', 'installing'].includes(phase);
    },
  });
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
  session.defaultSession.on('will-download', (_event, item) => {
    item.setSaveDialogOptions({ defaultPath: item.getFilename() });
  });
  app.on('second-instance', () => {
    if (quitting || !mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
  setupUpdates();
  installMenu();
  mainWindow = createWindow();
  await startAttempt();
  if (!quitting) updates?.start();
});

app.on('window-all-closed', () => void requestQuit());
app.on('activate', () => {
  if (quitting) return;
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
app.on('will-quit', () => updates?.dispose());
