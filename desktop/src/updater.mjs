const STARTUP_DELAY_MS = 30_000;
const CHECK_INTERVAL_MS = 24 * 60 * 60 * 1_000;

/**
 * Offer updates before downloading. The caller owns preferences and performs
 * local cleanup before requestInstall quits and installs the downloaded update.
 *
 * @param {{
 *   updater: import('node:events').EventEmitter & { checkForUpdates(): Promise<unknown>, downloadUpdate(): Promise<unknown> },
 *   message?: (options: { title: string, message: string, detail?: string, buttons: string[] }) => Promise<number | { response: number }> | number | { response: number },
 *   requestInstall?: () => Promise<boolean> | boolean,
 *   getSkippedVersion?: () => string | undefined,
 *   skipVersion?: (version: string) => void,
 *   onState?: (state: { phase: string, percent?: number, version?: string }) => void,
 *   setTimeoutFn?: typeof setTimeout,
 *   clearTimeoutFn?: typeof clearTimeout,
 *   setIntervalFn?: typeof setInterval,
 *   clearIntervalFn?: typeof clearInterval,
 * }} options
 */
export function createUpdater({
  updater,
  message = () => ({ response: 1 }),
  requestInstall = () => false,
  getSkippedVersion = () => undefined,
  skipVersion = () => {},
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
  let attempt;
  let state = { phase: 'idle' };

  // electron-updater's channel setter enables downgrades. Restore our policy
  // after choosing the release feed.
  updater.autoDownload = false;
  updater.autoInstallOnAppQuit = false;
  updater.allowPrerelease = true;
  updater.channel = 'latest';
  updater.allowDowngrade = false;

  function publish(next) {
    if (disposed) return;
    state = next;
    onState({ ...state });
  }

  function reportFailure() {
    if (disposed || attempt?.failureShown) return;
    if (attempt) attempt.failureShown = true;
    publish({ phase: 'error' });
    if (!attempt?.manual && !attempt?.accepted) return;
    void Promise.resolve(message({
      title: 'Could not update Frisket Desktop',
      message: 'Frisket Desktop could not check for or download an update. Check your internet connection and try again later.',
      buttons: ['OK'],
    })).catch(() => {});
  }

  async function offerUpdate() {
    const { version, phase } = state;
    if (disposed || !version || (!attempt.manual && getSkippedVersion() === version)) return;
    const result = await message({
      title: 'Frisket Desktop update',
      message: 'A new version of Frisket Desktop is available.',
      detail: `Version ${version}. ${phase === 'ready' ? 'The update is downloaded.' : 'Frisket will download the update.'} Updating will restart the app.`,
      buttons: ['Update now', 'Later', 'Skip this version'],
    });
    if (disposed) return;
    const response = typeof result === 'number' ? result : result?.response;
    if (response === 2) {
      try { skipVersion(version); }
      catch {
        await message({
          title: 'Could not save update preference',
          message: 'Frisket could not remember this choice. You may be offered this version again.',
          buttons: ['OK'],
        });
      }
      return;
    }
    if (response !== 0) return;
    attempt.accepted = true;
    if (phase !== 'ready') {
      publish({ phase: 'downloading', version });
      await updater.downloadUpdate();
    }
    if (disposed || state.phase !== 'ready') return;
    // A refused shutdown keeps the download ready for a later retry.
    if (await requestInstall() !== false) publish({ phase: 'installing', version });
  }

  const listeners = {
    'checking-for-update': () => publish({ phase: 'checking' }),
    'update-available': (info) => publish({ phase: 'available', version: info.version }),
    'update-not-available': () => publish({ phase: 'up-to-date' }),
    'download-progress': (progress) => {
      const percent = Number(progress?.percent);
      publish({ phase: 'downloading', version: state.version, ...(Number.isFinite(percent) ? { percent } : {}) });
    },
    'update-downloaded': (info) => publish({ phase: 'ready', version: info.version }),
    error: reportFailure,
  };
  for (const [event, listener] of Object.entries(listeners)) updater.on(event, listener);

  function check({ manual = false } = {}) {
    if (disposed || state.phase === 'installing') return Promise.resolve();
    if (checkTask) {
      if (manual) attempt.manual = true;
      return checkTask;
    }
    attempt = { manual, accepted: false, failureShown: false };
    // Own the whole interaction, including the dialog and download, so manual
    // clicks and scheduled checks cannot open competing prompts or installers.
    checkTask = Promise.resolve().then(async () => {
      if (state.phase !== 'ready') {
        publish({ phase: 'checking' });
        await updater.checkForUpdates();
      }
      if (disposed) return;
      if (['available', 'ready'].includes(state.phase)) await offerUpdate();
      else if (state.phase === 'up-to-date' && attempt.manual) {
        await message({
          title: 'Frisket is up to date',
          message: 'You already have the latest version of Frisket Desktop.',
          buttons: ['OK'],
        });
      }
    }).catch(reportFailure).finally(() => {
      attempt = undefined;
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
    for (const [event, listener] of Object.entries(listeners)) updater.removeListener(event, listener);
  }

  return { check, start, dispose, get state() { return { ...state }; } };
}
