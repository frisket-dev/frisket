const STARTUP_DELAY_MS = 30_000;
const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1_000;

/**
 * Connect electron-updater to the desktop shell without giving it ownership of
 * application shutdown. The caller performs local cleanup before requestInstall
 * quits and installs the downloaded update.
 *
 * @param {{
 *   updater: import('node:events').EventEmitter & { checkForUpdates(): Promise<unknown> },
 *   notify?: (title: string, body: string, onClick: () => void) => void,
 *   message?: (options: { title: string, message: string, buttons: string[] }) => Promise<number | { response: number }> | number | { response: number },
 *   requestInstall?: () => Promise<boolean> | boolean,
 *   onState?: (state: { phase: string, percent?: number, version?: string }) => void,
 *   setTimeoutFn?: typeof setTimeout,
 *   clearTimeoutFn?: typeof clearTimeout,
 *   setIntervalFn?: typeof setInterval,
 *   clearIntervalFn?: typeof clearInterval,
 * }} options
 */
export function createUpdater({
  updater,
  notify = () => {},
  message = () => ({ response: 1 }),
  requestInstall = () => false,
  onState = () => {},
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
  setIntervalFn = setInterval,
  clearIntervalFn = clearInterval,
}) {
  let disposed = false;
  let started = false;
  let startupTimer;
  let intervalTimer;
  let checkTask;
  let readyDialogTask;
  let installTask;
  let attempt;
  let state = { phase: 'idle' };

  // electron-updater's channel setter enables downgrades. Set it first, then
  // restore the policy we want for production releases.
  updater.autoDownload = true;
  updater.autoInstallOnAppQuit = false;
  updater.allowPrerelease = true;
  updater.channel = 'latest';
  updater.allowDowngrade = false;

  function publish(next) {
    if (disposed) return;
    state = next;
    onState({ ...state });
  }

  function updateVersion(info) {
    return typeof info?.version === 'string' ? info.version : undefined;
  }

  function manualUnavailable() {
    if (!attempt?.manual || attempt.unavailableShown) return;
    attempt.unavailableShown = true;
    void Promise.resolve(message({
      title: 'Frisket is up to date',
      message: 'You already have the latest version of Frisket Desktop.',
      buttons: ['OK'],
    })).catch(() => {});
  }

  function reportFailure(error, failedAttempt = attempt) {
    if (disposed || failedAttempt?.failureShown) return;
    if (failedAttempt) failedAttempt.failureShown = true;
    publish({ phase: 'error' });
    if (!failedAttempt?.manual) return;
    const detail = error instanceof Error && error.message ? ` ${error.message}` : '';
    void Promise.resolve(message({
      title: 'Could not check for updates',
      message: `Frisket Desktop could not check for updates.${detail}`,
      buttons: ['OK'],
    })).catch(() => {});
  }

  async function showReadyDialog() {
    if (disposed || state.phase !== 'ready') return;
    if (readyDialogTask) return readyDialogTask;
    readyDialogTask = (async () => {
      const result = await message({
        title: 'Update ready to install',
        message: 'A Frisket Desktop update has downloaded and is ready to install.',
        buttons: ['Restart to update', 'Later'],
      });
      const response = typeof result === 'number' ? result : result?.response;
      if (response !== 0 || disposed) return;
      if (installTask) return installTask;
      installTask = Promise.resolve(requestInstall())
        .then((installed) => {
          if (installed !== false) publish({ phase: 'installing', version: state.version });
        })
        .catch(() => {})
        .finally(() => { installTask = undefined; });
      return installTask;
    })().finally(() => { readyDialogTask = undefined; });
    return readyDialogTask;
  }

  const listeners = {
    checking: () => publish({ phase: 'checking' }),
    available: (info) => publish({ phase: 'downloading', version: updateVersion(info) }),
    unavailable: () => {
      publish({ phase: 'up-to-date' });
      manualUnavailable();
    },
    progress: (progress) => {
      const percent = Number(progress?.percent);
      publish({ phase: 'downloading', ...(Number.isFinite(percent) ? { percent } : {}), ...(state.version ? { version: state.version } : {}) });
    },
    downloaded: (info) => {
      const version = updateVersion(info) ?? state.version;
      publish({ phase: 'ready', ...(version ? { version } : {}) });
      try {
        notify('Update ready', 'A Frisket Desktop update has downloaded and is ready to install.', () => { void showReadyDialog(); });
      } catch {}
    },
    error: (error) => reportFailure(error),
  };

  updater.on('checking-for-update', listeners.checking);
  updater.on('update-available', listeners.available);
  updater.on('update-not-available', listeners.unavailable);
  updater.on('download-progress', listeners.progress);
  updater.on('update-downloaded', listeners.downloaded);
  updater.on('error', listeners.error);

  function check({ manual = false } = {}) {
    if (disposed) return Promise.resolve();
    if (state.phase === 'ready' && manual) return showReadyDialog();
    if (checkTask) return checkTask;

    attempt = { manual, failureShown: false, unavailableShown: false };
    publish({ phase: 'checking' });
    const currentAttempt = attempt;
    checkTask = Promise.resolve()
      .then(() => updater.checkForUpdates())
      .then(async (result) => {
        // Some electron-updater versions expose the automatic download as a
        // result promise. Awaiting and handling it keeps the next timer/manual
        // check from starting a duplicate download and consumes its rejection.
        if (result?.downloadPromise && typeof result.downloadPromise.then === 'function') {
          try { await result.downloadPromise; } catch (error) { reportFailure(error, currentAttempt); }
        }
        return result;
      })
      .catch((error) => { reportFailure(error, currentAttempt); })
      .finally(() => {
        if (attempt === currentAttempt) attempt = undefined;
        checkTask = undefined;
      });
    return checkTask;
  }

  function start() {
    if (disposed || started) return;
    started = true;
    startupTimer = setTimeoutFn(() => { void check(); }, STARTUP_DELAY_MS);
    intervalTimer = setIntervalFn(() => { void check(); }, CHECK_INTERVAL_MS);
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    if (startupTimer !== undefined) clearTimeoutFn(startupTimer);
    if (intervalTimer !== undefined) clearIntervalFn(intervalTimer);
    for (const [event, listener] of Object.entries({
      'checking-for-update': listeners.checking,
      'update-available': listeners.available,
      'update-not-available': listeners.unavailable,
      'download-progress': listeners.progress,
      'update-downloaded': listeners.downloaded,
      error: listeners.error,
    })) updater.removeListener(event, listener);
  }

  return {
    check,
    start,
    dispose,
    get state() { return { ...state }; },
  };
}

export const updaterTiming = { STARTUP_DELAY_MS, CHECK_INTERVAL_MS };
