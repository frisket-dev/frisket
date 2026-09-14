import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';
import { createUpdater } from '../src/updater.mjs';

class FakeUpdater extends EventEmitter {
  checks = 0;
  result = {};
  failure;

  async checkForUpdates() {
    this.checks += 1;
    if (this.failure) throw this.failure;
    return this.result;
  }
}

function createHarness({ response = 1, requestInstall = async () => true } = {}) {
  const updater = new FakeUpdater();
  const states = [];
  const notifications = [];
  const messages = [];
  const timers = { timeouts: [], intervals: [], clearedTimeouts: [], clearedIntervals: [] };
  const controller = createUpdater({
    updater,
    notify: (title, body, onClick) => notifications.push({ title, body, onClick }),
    message: async (options) => { messages.push(options); return response; },
    requestInstall,
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
  return { updater, controller, states, notifications, messages, timers };
}

test('configures automatic background downloads without install-on-quit or downgrades', () => {
  const { updater } = createHarness();
  assert.equal(updater.autoDownload, true);
  assert.equal(updater.autoInstallOnAppQuit, false);
  assert.equal(updater.allowPrerelease, true);
  assert.equal(updater.channel, 'latest');
  assert.equal(updater.allowDowngrade, false);
});

test('starts one delayed check and a daily recurring check', async () => {
  const { controller, updater, timers } = createHarness();
  controller.start();
  controller.start();
  assert.deepEqual(timers.timeouts.map(({ delay }) => delay), [30_000]);
  assert.deepEqual(timers.intervals.map(({ delay }) => delay), [24 * 60 * 60 * 1_000]);
  timers.timeouts[0].callback();
  await new Promise(setImmediate);
  timers.intervals[0].callback();
  await new Promise(setImmediate);
  assert.equal(updater.checks, 2);
});

test('coalesces overlapping checks and lets a manual click receive that check feedback', async () => {
  const { controller, updater, messages } = createHarness();
  let resolveCheck;
  updater.checkForUpdates = () => {
    updater.checks += 1;
    return new Promise((resolve) => { resolveCheck = resolve; });
  };
  const background = controller.check();
  const manual = controller.check({ manual: true });
  await new Promise(setImmediate);
  updater.emit('update-not-available');
  resolveCheck({});
  await Promise.all([background, manual]);
  assert.equal(updater.checks, 1);
  assert.equal(messages[0].title, 'Frisket is up to date');
});

test('publishes download progress and gives a ready notification whose click opens the install dialog', async () => {
  const { updater, controller, states, notifications, messages } = createHarness();
  updater.emit('update-available', { version: '1.2.3-alpha.1' });
  updater.emit('download-progress', { percent: 47.5 });
  updater.emit('update-downloaded', { version: '1.2.3-alpha.1' });

  assert.deepEqual(states.at(-2), { phase: 'downloading', percent: 47.5, version: '1.2.3-alpha.1' });
  assert.deepEqual(controller.state, { phase: 'ready', version: '1.2.3-alpha.1' });
  assert.equal(notifications.length, 1);
  notifications[0].onClick();
  await new Promise(setImmediate);
  assert.deepEqual(messages[0].buttons, ['Restart to update', 'Later']);
  await controller.check();
  assert.equal(updater.checks, 0);
  assert.equal(controller.state.phase, 'ready');
});

test('Later keeps an update ready and manual check opens its restart dialog', async () => {
  const { updater, controller, messages } = createHarness({ response: 1 });
  updater.emit('update-downloaded', { version: '1.2.3' });
  await controller.check({ manual: true });
  assert.equal(controller.state.phase, 'ready');
  assert.equal(messages.length, 1);
  assert.deepEqual(messages[0].buttons, ['Restart to update', 'Later']);
});

test('explicit restart requests cleanup-aware install once and preserves ready state on refusal', async () => {
  let installs = 0;
  const { updater, controller } = createHarness({
    response: 0,
    requestInstall: async () => { installs += 1; return false; },
  });
  updater.emit('update-downloaded', { version: '1.2.3' });
  await Promise.all([controller.check({ manual: true }), controller.check({ manual: true })]);
  assert.equal(installs, 1);
  assert.deepEqual(controller.state, { phase: 'ready', version: '1.2.3' });
});

test('manual checks report unavailable and download failures while background failures stay quiet', async () => {
  const unavailable = createHarness();
  const unavailableCheck = unavailable.controller.check({ manual: true });
  unavailable.updater.emit('update-not-available');
  await unavailableCheck;
  assert.equal(unavailable.messages[0].title, 'Frisket is up to date');

  const failed = createHarness();
  failed.updater.result = { downloadPromise: Promise.reject(new Error('offline')) };
  await failed.controller.check({ manual: true });
  assert.equal(failed.controller.state.phase, 'error');
  assert.match(failed.messages[0].message, /Check your internet connection/);

  const background = createHarness();
  background.updater.failure = new Error('offline');
  await background.controller.check();
  assert.equal(background.controller.state.phase, 'error');
  assert.equal(background.messages.length, 0);
});

test('dispose removes updater listeners and cancels scheduled work', () => {
  const { updater, controller, notifications, timers } = createHarness();
  controller.start();
  controller.dispose();
  updater.emit('update-downloaded', { version: '1.2.3' });
  assert.equal(notifications.length, 0);
  assert.equal(timers.clearedTimeouts.length, 1);
  assert.equal(timers.clearedIntervals.length, 1);
  assert.equal(updater.listenerCount('error'), 0);
});
