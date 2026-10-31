import { mkdir, writeFile, readFile } from 'node:fs/promises';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { app, BrowserWindow, Menu, dialog, session, shell, protocol, clipboard, ipcMain, screen } from 'electron';
import electronUpdater from 'electron-updater';
import { installStartupBridge, canRetryStartup, resizeStartupWindow } from './startup-bridge.mjs';
import { prepareRuntime } from './provision.mjs';
import { appUrl, installProtocol, APP_ORIGIN, APP_SCHEME } from './protocol.mjs';
import { startBackend } from './backend.mjs';
import { startupDiagnostics } from './startup-diagnostics.mjs';
import { CleanupError } from './errors.mjs';
import { createUpdater } from './updater.mjs';
import { createUpdateProgress } from './update-progress.mjs';
import { updatePreferences } from './update-preferences.mjs';
import { createBackendStopper, shutdownDesktop } from './shutdown.mjs';
import { createLaunchIntents, installLaunchDelivery } from './launch-intent.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(here, '..');
if (process.platform === 'win32') app.setAppUserModelId('dev.frisket.desktop');
const startupLog = startupDiagnostics({ version: app.getVersion(), platform: process.platform, arch: process.arch });
let mainWindow;
let startupWindow;
let retiringStartup = false;
let detailsWindow;
let startupVisible = true;
let openingHome = false;
let homeBounds = { width: 1240, height: 840 };
let detailsOpen = false;
let backend;
let startupTask;
let provisioningController;
let quitting = false;
let quitReady = false;
let recoveryTask;
let cleanupFailed = false;
let updates;
let updatesStarted = false;
const launchIntents = createLaunchIntents({
  navigate: async destination => {
    if (quitting || startupVisible || !mainWindow || mainWindow.isDestroyed()) return;
    await mainWindow.loadURL(destination);
    if (!quitting && !startupVisible && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  },
});
const ownsInstance = installLaunchDelivery({
  app, intents: launchIntents, argv: process.argv,
  focus: () => {
    if (quitting) return;
    const window = startupVisible ? startupWindow : mainWindow;
    if (!window || window.isDestroyed()) return;
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
  },
});
const updateProgress = createUpdateProgress({
  BrowserWindow, getParent: () => mainWindow, pagePath: path.join(desktopRoot, 'ui', 'update.html'),
  createQuitMenu: () => Menu.buildFromTemplate([{
    label: 'File', submenu: [{ label: 'Quit Frisket', accelerator: 'CmdOrCtrl+Q', click: () => void requestQuit() }],
  }]),
});
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
  window.webContents.on('render-process-gone', (_event, details) => {
    if (!quitting) void recover(new Error(`The workspace window stopped (${details.reason}).`));
  });
  return window;
}

function createStartupCard(parent) {
  const window = new BrowserWindow({
    ...(parent ? { parent } : {}), width: 360, height: 390, frame: false,
    resizable: false, maximizable: false, backgroundColor: '#ffffff',
    title: 'Preparing Frisket Desktop', show: false,
    webPreferences: { preload: path.join(here, 'startup-preload.cjs'), sandbox: true, contextIsolation: true, nodeIntegration: false, webviewTag: false },
  });
  protectWindow(window);
  return window;
}

async function showStartup() {
  launchIntents.setReady(false);
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.stop();
  startupVisible = true;
  openingHome = false;
  detailsOpen = false;
  if (!startupWindow || startupWindow.isDestroyed()) {
    startupWindow = createStartupCard();
    startupWindow.on('close', event => {
      if (quitting || retiringStartup) return;
      event.preventDefault();
      void requestQuit();
    });
  }
  mainWindow?.hide();
  startupWindow.center();
  await startupWindow.loadFile(path.join(desktopRoot, 'ui', 'startup.html'));
  startupWindow.show();
}

function publishStartup() {
  for (const window of [startupWindow, detailsWindow]) {
    if (!window || window.isDestroyed() || window.webContents.getURL() !== startupPageUrl()) continue;
    window.webContents.send('startup:state', { ...startupLog.snapshot(), detailsOnly: window === detailsWindow, detailsOpen: window === detailsWindow || detailsOpen });
  }
}

function startupEvent(event) {
  startupLog.event(event);
  publishStartup();
}

function setStartupStatus({ message }) {
  startupEvent({ type: 'activity', message });
}

function setupActivity(message) {
  startupLog.record(message);
  publishStartup();
}

function startupPageUrl() {
  return pathToFileURL(path.join(desktopRoot, 'ui', 'startup.html')).href;
}

async function openHome(window) {
  launchIntents.setReady(false);
  window.webContents.stop();
  openingHome = true;
  const area = (Number.isFinite(homeBounds.x) && Number.isFinite(homeBounds.y) ? screen.getDisplayMatching(homeBounds) : screen.getPrimaryDisplay()).workArea;
  const bounds = { ...homeBounds, width: Math.min(homeBounds.width, area.width), height: Math.min(homeBounds.height, area.height) };
  bounds.x = Math.max(area.x, Math.min(bounds.x ?? area.x + Math.round((area.width - bounds.width) / 2), area.x + area.width - bounds.width));
  bounds.y = Math.max(area.y, Math.min(bounds.y ?? area.y + Math.round((area.height - bounds.height) / 2), area.y + area.height - bounds.height));
  window.setBounds(bounds);
  await window.loadURL(`${APP_ORIGIN}/`);
  const card = startupWindow;
  if (quitting || window.isDestroyed()) return;
  startupVisible = false;
  openingHome = false;
  window.show();
  window.focus();
  if (card && !card.isDestroyed()) {
    retiringStartup = true;
    card.close();
    retiringStartup = false;
  }
  startupWindow = undefined;
  launchIntents.setReady(true);
}

async function openStartupDetails() {
  if (startupVisible) { detailsOpen = true; publishStartup(); return; }
  if (detailsWindow && !detailsWindow.isDestroyed()) { detailsWindow.show(); return; }
  detailsWindow = createStartupCard(mainWindow);
  detailsWindow.on('closed', () => { detailsWindow = undefined; });
  await detailsWindow.loadFile(path.join(desktopRoot, 'ui', 'startup.html'));
  detailsWindow.show();
}

async function saveStartupDiagnostics() {
  await writeFile(path.join(app.getPath('userData'), 'startup-diagnostics.txt'), startupLog.report(), { mode: 0o600 }).catch(() => {});
}

async function launch() {
  const dataPath = app.getPath('userData');
  const workspace = path.join(dataPath, 'workspace');
  await mkdir(workspace, { recursive: true });
  provisioningController = new AbortController();
  const runtime = await prepareRuntime({
    resourcesPath: resourcesPath(), dataPath, signal: provisioningController.signal, onProgress: setStartupStatus, onActivity: setupActivity, onEvent: startupEvent,
  });
  if (quitting) return;
  setStartupStatus({ phase: 'workspace', message: 'Opening databases' });
  backend = await startBackend({
    runtime, resourcesPath: resourcesPath(), workspace, profileDir: dataPath,
    onFailure: (error) => void recover(error), onActivity: setupActivity, signal: provisioningController.signal,
  });
  if (quitting) {
    await stopBackend();
    return;
  }
  installProtocol({ protocol }, backend);
  await openHome(mainWindow);
  startupEvent({ type: 'step-end', step: 'open', result: 'ok' });
  await saveStartupDiagnostics();
  if (!quitting && !updatesStarted) { updatesStarted = true; void updates?.start(); }
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
    startupLog.record(`Failed: ${error instanceof Error ? error.message : String(error)}`);
    if (!startupVisible) await showStartup();
    const step = startupLog.snapshot().currentStep;
    if (step) startupEvent({ type: 'step-end', step, result: 'failed' });
    startupEvent({ type: 'failure', error });
    await saveStartupDiagnostics();
  })();
  try { await recoveryTask; } finally {
    recoveryTask = undefined;
    if (!startupTask && !quitting) startupEvent({ type: 'stopped', retryable: !cleanupFailed });
  }
}

function startAttempt() {
  if (startupTask) return startupTask;
  startupTask = (async () => {
    try {
      startupLog.begin();
      await showStartup();
      if (!quitting) await launch();
    } catch (error) {
      if (quitting && error instanceof CleanupError) throw error;
      if (!quitting) await recover(error);
    } finally {
      startupTask = undefined;
      provisioningController = undefined;
      if (!quitting && startupLog.snapshot().status === 'failed') startupEvent({ type: 'stopped', retryable: !cleanupFailed });
    }
  })();
  return startupTask;
}

async function requestQuit(installUpdate = false) {
  if (quitting) return;
  quitting = true;
  updateProgress.close();
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
      // Cleanup starting is not permission for a second quit request to exit.
      // Only resume Electron's normal lifecycle after our services have stopped.
      quitReady = true;
      if (!installUpdate) { app.quit(); return; }
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
      label: 'Startup details', click: () => void openStartupDetails(),
    }, {
      id: 'desktop-update', label: 'Check for Updates…', enabled: Boolean(updates),
      click: () => void updates?.check({ manual: true }),
    }, {
      id: 'desktop-update-automatic', label: 'Automatically Check for Updates',
      type: 'checkbox', enabled: Boolean(updates), checked: updates?.automaticChecksEnabled ?? false,
      click: (item) => {
        updates?.setAutomaticChecks(item.checked);
        item.checked = updates?.automaticChecksEnabled ?? false;
      },
    }] },
  ]));
}

function setupUpdates() {
  if (!app.isPackaged || !['darwin', 'win32'].includes(process.platform)) return;
  updates = createUpdater({
    updater: electronUpdater.autoUpdater,
    ...updatePreferences(path.join(app.getPath('userData'), 'update-preferences.json')),
    requestInstall: () => requestQuit(true),
    message: async (options) => (await dialog.showMessageBox(mainWindow, {
      ...options, defaultId: options.defaultId ?? 0,
      cancelId: options.cancelId ?? Math.max(0, options.buttons.indexOf('Later')),
    })).response,
    onAutomaticChecksChange: (enabled) => {
      const item = Menu.getApplicationMenu()?.getMenuItemById('desktop-update-automatic');
      if (item) item.checked = enabled;
    },
    onState: (state) => {
      if (!quitting) updateProgress.update(state);
      const { phase } = state;
      const item = Menu.getApplicationMenu()?.getMenuItemById('desktop-update');
      if (!item) return;
      item.label = phase === 'ready' ? 'Restart to update…'
        : phase === 'available' ? 'Update available…'
        : phase === 'checking' ? 'Checking for updates…'
          : phase === 'downloading' ? 'Downloading update…'
            : 'Check for Updates…';
      item.enabled = !quitting && !['checking', 'downloading', 'installing'].includes(phase);
    },
  });
}

app.whenReady().then(async () => {
  if (!ownsInstance) return;
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
  setupUpdates();
  installMenu();
  try {
    const saved = JSON.parse(await readFile(path.join(app.getPath('userData'), 'window-bounds.json'), 'utf8'));
    if ([saved.width, saved.height].every(value => Number.isFinite(value) && value >= 600 && value <= 10000)
      && [saved.x, saved.y].every(Number.isFinite)) homeBounds = saved;
  } catch {}
  mainWindow = createWindow();
  installStartupBridge({
    ipcMain, pageUrl: startupPageUrl(), findWindow: contents => [startupWindow, detailsWindow].find(window => window && window.webContents === contents),
    snapshot: window => ({ ...startupLog.snapshot(), detailsOnly: window === detailsWindow, detailsOpen: window === detailsWindow || detailsOpen }),
    retry: window => {
      if (window !== startupWindow || !canRetryStartup({ visible: startupVisible, busy: Boolean(startupTask || recoveryTask), cleanupFailed, quitting, retryable: startupLog.snapshot().retryable })) return;
      void startAttempt();
    },
    close: window => { if (window === detailsWindow) window.close(); else void requestQuit(); },
    copy: () => clipboard.writeText(startupLog.report()),
    resize: (window, height) => { if (window === detailsWindow || (window === startupWindow && startupVisible && !openingHome)) resizeStartupWindow(window, height); },
    details: (window, open) => { if (window === startupWindow) detailsOpen = open; },
  });
  const saveBounds = () => {
    if (startupVisible || mainWindow.isDestroyed() || !mainWindow.isNormal()) return;
    homeBounds = mainWindow.getBounds();
    void writeFile(path.join(app.getPath('userData'), 'window-bounds.json'), JSON.stringify(homeBounds), { mode: 0o600 }).catch(() => {});
  };
  mainWindow.on('resize', saveBounds);
  mainWindow.on('move', saveBounds);
  await startAttempt();
});

app.on('window-all-closed', () => void requestQuit());
app.on('activate', () => {
  if (quitting) return;
  if (!mainWindow || mainWindow.isDestroyed()) {
    mainWindow = createWindow();
    if (backend) void openHome(mainWindow);
    else void startAttempt();
  }
});
app.on('before-quit', (event) => {
  if (quitReady) return;
  event.preventDefault();
  void requestQuit();
});
app.on('will-quit', () => { updateProgress.close(); updates?.dispose(); });
