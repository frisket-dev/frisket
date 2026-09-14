import assert from 'node:assert/strict';
import test from 'node:test';
import { createBackendStopper, shutdownDesktop } from '../src/shutdown.mjs';
import { CleanupError } from '../src/errors.mjs';

for (const rejected of [false, true]) {
  test(`restart waits for recovery's detached backend cleanup (${rejected ? 'failure' : 'success'})`, async () => {
    let finishStop;
    let rejectStop;
    const stopped = new Promise((resolve, reject) => { finishStop = resolve; rejectStop = reject; });
    let backend = { stop: () => stopped };
    const stopBackend = createBackendStopper(() => {
      const current = backend;
      backend = undefined;
      return current;
    });
    const recovery = stopBackend();
    assert.equal(backend, undefined);
    const events = [];
    const shutdown = shutdownDesktop({
      abortStartup() {}, stopBackend,
      finish: () => events.push('install'), fail: () => events.push('failed'),
    });
    await new Promise(setImmediate);
    assert.deepEqual(events, []);
    if (rejected) rejectStop(new CleanupError('recovery cleanup failed'));
    else finishStop();
    await Promise.allSettled([recovery, shutdown]);
    assert.deepEqual(events, [rejected ? 'failed' : 'install']);
  });
}

test('installation waits for backend cleanup and in-progress setup', async () => {
  const events = [];
  let stop;
  let setup;
  const stopping = new Promise((resolve) => { stop = resolve; });
  const startupTask = new Promise((resolve) => { setup = resolve; });
  const shutdown = shutdownDesktop({
    abortStartup: () => events.push('abort'),
    stopBackend: () => stopping,
    startupTask,
    finish: () => events.push('install'),
    fail: assert.fail,
  });
  stop();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(events, ['abort']);
  setup();
  assert.equal(await shutdown, true);
  assert.deepEqual(events, ['abort', 'install']);
});

for (const failure of ['backend', 'startup', 'prior cleanup', 'timeout']) {
  test(`${failure} failure prevents installation`, async () => {
    const errors = [];
    const result = await shutdownDesktop({
      abortStartup() {},
      stopBackend: () => {
        if (failure === 'backend') throw new Error('stop failed');
        if (failure === 'timeout') return new Promise(() => {});
      },
      startupTask: failure === 'startup' ? Promise.reject(new Error('setup failed')) : undefined,
      cleanupFailed: failure === 'prior cleanup',
      finish: () => assert.fail('must not install or exit successfully'),
      fail: (error) => errors.push(error),
      timeoutMs: 10,
    });
    assert.equal(result, false);
    assert.equal(errors.length, 1);
  });
}
