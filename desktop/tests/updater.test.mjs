import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';
import { createUpdater } from '../src/updater.mjs';

class FakeUpdater extends EventEmitter {
  checks = 0;
  downloads = 0;
  version = '1.2.3';
  failure;
  downloadFailure;

  async checkForUpdates() {
    this.checks += 1;
    if (this.failure) throw this.failure;
    this.emit(this.version ? 'update-available' : 'update-not-available', { version: this.version });
    return {};
  }

  async downloadUpdate() {
    this.downloads += 1;
    if (this.downloadFailure) throw this.downloadFailure;
    this.emit('download-progress', { percent: 47.5 });
    this.emit('update-downloaded', { version: this.version });
    return [];
  }
}

function createHarness({ response = 1, requestInstall, preferences = {}, automaticChecks = true } = {}) {
  const updater = new FakeUpdater();
  const states = [];
  const messages = [];
  const installs = [];
  const automaticCheckWrites = [];
  const automaticCheckChanges = [];
  const timers = { timeouts: [], intervals: [], clearedTimeouts: [], clearedIntervals: [] };
  const controller = createUpdater({
    updater,
    getSkippedVersion: () => preferences.skippedVersion,
    skipVersion: (version) => { preferences.skippedVersion = version; },
    getAutomaticChecks: () => automaticChecks,
    setAutomaticChecks: (enabled) => { automaticChecks = enabled; automaticCheckWrites.push(enabled); },
    onAutomaticChecksChange: (enabled) => automaticCheckChanges.push(enabled),
    message: async (options) => { messages.push(options); return typeof response === 'function' ? response(options) : response; },
    requestInstall: async () => { installs.push(true); return requestInstall ? requestInstall() : true; },
    onState: (state) => states.push(state),
    setTimeoutFn: (callback, delay) => {
      const timer = { callback, delay };
      timers.timeouts.push(timer);
      return timer;
    },
    clearTimeoutFn: (timer) => timers.clearedTimeouts.push(timer),
    setIntervalFn: (callback, delay) => {
      const timer = { callback, delay };
      timers.intervals.push(timer);
      return timer;
    },
    clearIntervalFn: (timer) => timers.clearedIntervals.push(timer),
  });
  return {
    updater, controller, states, messages, installs, timers, preferences,
    automaticCheckWrites, automaticCheckChanges,
    get automaticChecks() { return automaticChecks; },
  };
}

test('checks without downloading until consent, and never installs on quit or downgrades', () => {
  const { updater } = createHarness();
  assert.equal(updater.autoDownload, false);
  assert.equal(updater.autoInstallOnAppQuit, false);
  assert.equal(updater.allowPrerelease, true);
  assert.equal(updater.channel, 'latest');
  assert.equal(updater.allowDowngrade, false);
});

test('startup and daily checks show availability; Later keeps the app running without downloading', async () => {
  const { controller, updater, timers, messages, installs } = createHarness();
  await controller.start();
  await controller.start();
  assert.deepEqual(timers.timeouts.map(({ delay }) => delay), [30_000]);
  assert.deepEqual(timers.intervals.map(({ delay }) => delay), [24 * 60 * 60 * 1_000]);
  timers.timeouts[0].callback();
  await new Promise(setImmediate);
  assert.equal(messages.length, 1);
  assert.match(messages[0].message, /new version of Frisket Desktop is available/i);
  assert.deepEqual(messages[0].buttons, ['Update now', 'Later', 'Skip this version']);
  assert.match(messages[0].detail, /1\.2\.3/);
  timers.intervals[0].callback();
  await new Promise(setImmediate);
  assert.equal(updater.checks, 2);
  assert.equal(messages.length, 2);
  assert.equal(updater.downloads, 0);
  assert.equal(installs.length, 0);
});

test('an unknown automatic-check preference is chosen once before timers or network work begin', async () => {
  const harness = createHarness({ automaticChecks: undefined, response: 0 });
  const start = harness.controller.start();
  assert.equal(harness.updater.checks, 0);
  await start;
  assert.equal(harness.messages[0].message, 'Check for updates automatically?');
  assert.deepEqual(harness.messages[0].buttons, ['Check automatically', 'Only when I ask']);
  assert.deepEqual(harness.automaticCheckWrites, [true]);
  assert.deepEqual(harness.automaticCheckChanges, [true]);
  assert.equal(harness.controller.automaticChecksEnabled, true);
  assert.deepEqual(harness.timers.timeouts.map(({ delay }) => delay), [30_000]);
  assert.deepEqual(harness.timers.intervals.map(({ delay }) => delay), [24 * 60 * 60 * 1_000]);
  await harness.controller.start();
  assert.equal(harness.messages.length, 1);
});

test('choosing manual-only keeps automatic timers off while manual checks still work without opting in', async () => {
  const harness = createHarness({ automaticChecks: undefined, response: 1 });
  await harness.controller.start();
  assert.deepEqual(harness.automaticCheckWrites, [false]);
  assert.equal(harness.controller.automaticChecksEnabled, false);
  assert.equal(harness.timers.timeouts.length, 0);
  assert.equal(harness.timers.intervals.length, 0);
  await harness.controller.check({ manual: true });
  assert.equal(harness.updater.checks, 1);
  assert.deepEqual(harness.automaticCheckWrites, [false]);
  assert.equal(harness.controller.automaticChecksEnabled, false);
});

test('changing the automatic-check choice cancels work, suppresses an open automatic offer, and rearms once', async () => {
  let respond;
  const harness = createHarness({
    response: () => new Promise((resolve) => { respond = resolve; }),
  });
  await harness.controller.start();
  harness.timers.timeouts[0].callback();
  await new Promise(setImmediate);
  assert.equal(harness.messages.length, 1);
  harness.controller.setAutomaticChecks(false);
  assert.equal(harness.controller.automaticChecksEnabled, false);
  assert.deepEqual(harness.automaticCheckWrites, [false]);
  assert.deepEqual(harness.automaticCheckChanges, [false]);
  assert.equal(harness.timers.clearedTimeouts.length, 1);
  assert.equal(harness.timers.clearedIntervals.length, 1);
  respond(0);
  await new Promise(setImmediate);
  assert.equal(harness.updater.downloads, 0);
  assert.equal(harness.installs.length, 0);
  harness.controller.setAutomaticChecks(true);
  assert.equal(harness.controller.automaticChecksEnabled, true);
  assert.deepEqual(harness.timers.timeouts.map(({ delay }) => delay), [30_000, 30_000]);
  assert.deepEqual(harness.timers.intervals.map(({ delay }) => delay), [24 * 60 * 60 * 1_000, 24 * 60 * 60 * 1_000]);
  harness.controller.setAutomaticChecks(true);
  assert.equal(harness.timers.timeouts.length, 2);
  assert.equal(harness.timers.intervals.length, 2);
});

test('one manual check offers, downloads and installs without a second menu click', async () => {
  const { controller, updater, states, messages, installs } = createHarness({ response: 0 });
  await controller.check({ manual: true });
  assert.equal(messages.length, 1);
  assert.equal(updater.downloads, 1);
  assert.equal(installs.length, 1);
  assert.ok(states.some((state) => state.phase === 'downloading' && state.percent === 47.5));
  assert.deepEqual(controller.state, { phase: 'installing', version: '1.2.3' });
});

test('coalesces checks and dialogs, including manual use while the automatic offer is open', async () => {
  let respond;
  const { controller, updater, messages, installs } = createHarness({
    response: () => new Promise((resolve) => { respond = resolve; }),
  });
  const automatic = controller.check();
  await new Promise(setImmediate);
  const manual = controller.check({ manual: true });
  assert.equal(messages.length, 1);
  respond(0);
  await Promise.all([automatic, manual]);
  assert.equal(updater.checks, 1);
  assert.equal(updater.downloads, 1);
  assert.equal(installs.length, 1);
});

test('Skip suppresses only that version across controllers; manual checks override it', async () => {
  const preferences = {};
  const first = createHarness({ preferences, response: 2 });
  await first.controller.check();
  assert.equal(preferences.skippedVersion, '1.2.3');
  assert.equal(first.updater.downloads, 0);
  first.controller.dispose();
  const next = createHarness({ preferences });
  await next.controller.check();
  assert.equal(next.messages.length, 0);
  await next.controller.check({ manual: true });
  assert.equal(next.messages.length, 1);
  next.updater.version = '1.2.4';
  await next.controller.check();
  assert.equal(next.messages.length, 2);
});

test('cleanup refusal keeps downloaded update ready and manual retry does not download again', async () => {
  const { controller, updater, installs } = createHarness({ response: 0, requestInstall: () => false });
  await controller.check({ manual: true });
  assert.equal(controller.state.phase, 'ready');
  await controller.check({ manual: true });
  assert.equal(installs.length, 2);
  assert.equal(updater.downloads, 1);
  assert.equal(controller.state.phase, 'ready');
});

test('manual unavailable and failed checks show feedback; automatic check failures stay quiet', async () => {
  const unavailable = createHarness();
  unavailable.updater.version = null;
  await unavailable.controller.check({ manual: true });
  assert.equal(unavailable.messages[0].title, 'Frisket is up to date');
  const failed = createHarness();
  failed.updater.failure = new Error('offline');
  await failed.controller.check({ manual: true });
  assert.equal(failed.controller.state.phase, 'error');
  assert.match(failed.messages[0].message, /Check your internet connection/);
  const background = createHarness();
  background.updater.failure = new Error('offline');
  await background.controller.check();
  assert.equal(background.controller.state.phase, 'error');
  assert.equal(background.messages.length, 0);
});

test('accepted automatic offer reports a download failure once and never installs', async () => {
  const { controller, updater, messages, installs } = createHarness({ response: 0 });
  updater.downloadUpdate = async () => {
    const error = new Error('download interrupted');
    updater.emit('error', error);
    throw error;
  };
  await controller.check();
  assert.equal(messages.length, 2);
  assert.match(messages[1].message, /Check your internet connection/);
  assert.equal(installs.length, 0);
  assert.equal(controller.state.phase, 'error');
});

test('dispose cancels scheduled work and prevents late dialog consent from downloading', async () => {
  let respond;
  const { updater, controller, timers, installs } = createHarness({
    response: () => new Promise((resolve) => { respond = resolve; }),
  });
  await controller.start();
  const check = controller.check();
  await new Promise(setImmediate);
  controller.dispose();
  respond(0);
  await check;
  assert.equal(updater.downloads, 0);
  assert.equal(installs.length, 0);
  assert.equal(timers.clearedTimeouts.length, 1);
  assert.equal(timers.clearedIntervals.length, 1);
  assert.equal(updater.listenerCount('error'), 0);
});
