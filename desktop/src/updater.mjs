const STARTUP_DELAY_MS = 30_000;
const CHECK_INTERVAL_MS = 24 * 60 * 60 * 1_000;

/**
 * Offer updates before downloading. The caller owns preferences and performs
 * local cleanup before requestInstall quits and installs the downloaded update.
 *
 * @param {{
 *   updater: import('node:events').EventEmitter & { checkForUpdates(): Promise<unknown>, downloadUpdate(): Promise<unknown> },
 *   message?: (options: { title: string, message: string, detail?: string, buttons: string[], defaultId?: number, cancelId?: number }) => Promise<number | { response: number }> | number | { response: number },
 *   requestInstall?: () => Promise<boolean> | boolean,
 *   getSkippedVersion?: () => string | undefined,
 *   skipVersion?: (version: string) => void,
 *   getAutomaticChecks?: () => boolean | undefined,
 *   setAutomaticChecks?: (enabled: boolean) => void,
 *   onAutomaticChecksChange?: (enabled: boolean) => void,
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
  getAutomaticChecks = () => undefined,
  setAutomaticChecks: saveAutomaticChecks = () => {},
  onAutomaticChecksChange = () => {},
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
  const savedAutomaticChecks = getAutomaticChecks();
  let choiceRecorded = typeof savedAutomaticChecks === 'boolean';
  let automaticChecks = savedAutomaticChecks === true;

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
    if (disposed || (!attempt.manual && !automaticChecks)) return;
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
    if (disposed || state.phase === 'installing' || (!manual && !automaticChecks)) return Promise.resolve();
    if (checkTask) {
      if (manual) attempt.manual = true;
      return checkTask;
    }
    attempt = { manual, accepted: false, failureShown: false };
    // Own the whole interaction, including the dialog and download, so manual
    // clicks and scheduled checks cannot open competing prompts or installers.
    checkTask = Promise.resolve().then(async () => {
      if (disposed || (!attempt.manual && !automaticChecks)) return;
      if (state.phase !== 'ready') {
        publish({ phase: 'checking' });
        await updater.checkForUpdates();
      }
      if (disposed || (!attempt.manual && !automaticChecks)) return;
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

  function clearTimers() {
    if (startupTimer !== undefined) clearTimeoutFn(startupTimer);
    if (intervalTimer !== undefined) clearIntervalFn(intervalTimer);
    startupTimer = undefined;
    intervalTimer = undefined;
  }

  function scheduleChecks() {
    clearTimers();
    if (disposed || !started || !automaticChecks) return;
    startupTimer = setTimeoutFn(() => { void check(); }, STARTUP_DELAY_MS);
    intervalTimer = setIntervalFn(() => { void check(); }, CHECK_INTERVAL_MS);
  }

  function setAutomaticChecks(enabled) {
    if (disposed) return false;
    if (choiceRecorded && automaticChecks === enabled) return true;
    automaticChecks = false;
    clearTimers();
    try {
      saveAutomaticChecks(enabled);
      choiceRecorded = true;
      automaticChecks = enabled;
    } catch {
      choiceRecorded = false;
      onAutomaticChecksChange(false);
      void Promise.resolve().then(() => message({
        title: 'Could not save update preference',
        message: 'Automatic update checks are off for this session. Frisket could not save your choice for the next launch.',
        buttons: ['OK'],
      })).catch(() => {});
      return false;
    }
    scheduleChecks();
    onAutomaticChecksChange(automaticChecks);
    return true;
  }

  async function start() {
    if (disposed || started) return;
    started = true;
    if (choiceRecorded) { scheduleChecks(); return; }
    try {
      const result = await message({
        title: 'Frisket Desktop updates',
        message: 'Check for updates automatically?',
        detail: 'Frisket can check online when you open the app and once a day while it stays open. You can change this in the Help menu, or check for updates there yourself.',
        buttons: ['Check automatically', 'Only when I ask'],
        defaultId: 1, cancelId: 1,
      });
      if (disposed || choiceRecorded) return;
      const response = typeof result === 'number' ? result : result?.response;
      setAutomaticChecks(response === 0);
    } catch {
      // A failed or interrupted choice never opts the user into network checks.
    }
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    clearTimers();
    for (const [event, listener] of Object.entries(listeners)) updater.removeListener(event, listener);
  }

  return {
    check, start, dispose, setAutomaticChecks,
    get automaticChecksEnabled() { return automaticChecks; },
    get state() { return { ...state }; },
  };
}
