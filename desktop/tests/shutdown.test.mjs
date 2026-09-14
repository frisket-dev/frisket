import assert from 'node:assert/strict';
import test from 'node:test';
import { shutdownDesktop } from '../src/shutdown.mjs';

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
