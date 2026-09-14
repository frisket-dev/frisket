/** Stop local services before exiting or handing the app to its updater. */
export async function shutdownDesktop({ abortStartup, stopBackend, startupTask, cleanupFailed, finish, fail, timeoutMs = 12_000 }) {
  let timeout;
  try {
    abortStartup();
    const results = await Promise.race([
      Promise.allSettled([Promise.resolve().then(stopBackend), startupTask]),
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error('Desktop shutdown timed out.')), timeoutMs);
      }),
    ]);
    if (cleanupFailed || results.some((result) => result.status === 'rejected')) {
      throw new Error('Frisket could not finish stopping its local services.');
    }
  } catch (error) {
    await fail(error);
    return false;
  } finally {
    clearTimeout(timeout);
  }
  await finish();
  return true;
}
/** Keep recovery and application shutdown waiting on the same backend cleanup. */
export function createBackendStopper(takeBackend) {
  let stopping;
  return () => {
    const backend = takeBackend();
    if (backend) {
      stopping = Promise.all([stopping, Promise.resolve().then(() => backend.stop())]);
    }
    return stopping;
  };
}
