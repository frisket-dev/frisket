import { randomBytes } from 'node:crypto';
import { spawn as nodeSpawn } from 'node:child_process';
import path from 'node:path';

const READY_TIMEOUT_MS = 20_000;
const STOP_TIMEOUT_MS = 10_000;
const OUTPUT_LIMIT = 8_192;

/** @typedef {{ python: string, env: Record<string, string> }} PreparedRuntime */
/** @typedef {{ host: '127.0.0.1', port: number, pid: number, token: string, stop: () => Promise<void> }} DesktopBackend */

/** @param {unknown} value */
export function parseReadyMessage(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const ready = /** @type {Record<string, unknown>} */ (value);
  if (Object.keys(ready).length !== 5 || !['schema', 'type', 'host', 'port', 'pid'].every((key) => key in ready)) return null;
  if (
    ready.schema !== 1 || ready.type !== 'ready' || ready.host !== '127.0.0.1' ||
    !Number.isInteger(ready.port) || ready.port < 1 || ready.port > 65535 ||
    !Number.isInteger(ready.pid) || ready.pid < 1
  ) return null;
  return /** @type {{ schema: 1, type: 'ready', host: '127.0.0.1', port: number, pid: number }} */ (ready);
}

/** The Python guardian treats a normal exit and pre-handler TERM death as proof. */
export function cleanupProved(code, signal) {
  return Number.isInteger(code) && code >= 0 || code === null && signal === 'SIGTERM';
}

/** @param {string} current @param {Buffer | string} chunk */
export function appendBounded(current, chunk) {
  const value = `${current}${chunk.toString()}`;
  return value.length > OUTPUT_LIMIT ? value.slice(-OUTPUT_LIMIT) : value;
}

/** @param {string} workspace @param {string} token */
export function startupMessage(workspace, token) {
  if (!path.isAbsolute(workspace)) throw new Error('desktop workspace must be absolute');
  return `${JSON.stringify({ schema: 1, workspace, token })}\n`;
}

/** @param {string} host @param {number} port */
export function healthUrl(host, port) {
  if (host !== '127.0.0.1' || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('invalid desktop backend address');
  }
  return `http://${host}:${port}/api/health`;
}

/**
 * Start the private backend under the bundled guardian. The startup deadline
 * covers readiness and authenticated health, and every failed attempt awaits
 * the guardian's cleanup acknowledgement before a caller can retry.
 * @param {{ runtime: PreparedRuntime, resourcesPath: string, workspace: string, electronPid?: number, spawnProcess?: typeof nodeSpawn, fetchImpl?: typeof fetch, onFailure?: (error: Error) => void, readyTimeoutMs?: number, stopTimeoutMs?: number }} options
 * @returns {Promise<DesktopBackend>}
 */
export async function startBackend(options) {
  const {
    runtime, resourcesPath, workspace, electronPid = process.pid,
    spawnProcess = nodeSpawn, fetchImpl = globalThis.fetch,
    onFailure = () => {}, readyTimeoutMs = READY_TIMEOUT_MS, stopTimeoutMs = STOP_TIMEOUT_MS,
  } = options;
  if (!runtime || !path.isAbsolute(runtime.python) || !runtime.env || !path.isAbsolute(resourcesPath) || !path.isAbsolute(workspace)) {
    throw new Error('invalid desktop backend configuration');
  }
  if (typeof fetchImpl !== 'function') throw new Error('desktop health check is unavailable');
  const guard = path.join(resourcesPath, 'python', 'frisket', 'runtime', '_guard.py');
  const bootstrap = path.join(resourcesPath, 'python', 'frisket', 'runtime', '_bootstrap.py');
  const token = randomBytes(32).toString('hex');
  const child = spawnProcess(runtime.python, [
    '-I', guard, String(electronPid), '8', runtime.python, '-I', bootstrap, 'desktop-server',
  ], { detached: true, env: runtime.env, stdio: ['pipe', 'pipe', 'pipe'] });
  const deadline = Date.now() + readyTimeoutMs;

  let stopped = false;
  let started = false;
  let failureReported = false;
  let stderr = '';
  let stdout = '';
  let stdoutBytes = 0;
  let closeOutcome = null;
  let lifecycleError = null;
  let readyResolve;
  let readyReject;
  const readiness = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  const fail = (message) => {
    const error = message instanceof Error ? message : new Error(message);
    lifecycleError ||= error;
    if (!started) readyReject(error);
    if (started && !stopped && !failureReported) {
      failureReported = true;
      onFailure(error);
    }
  };
  const closed = new Promise((resolve) => {
    child.once('close', (code, signal) => {
      closeOutcome = { code, signal };
      if (!stopped) {
        const detail = stderr.trim() ? ` ${redact(stderr.trim(), token)}` : '';
        fail(`Desktop backend exited unexpectedly.${detail || (signal ? ` (${signal})` : code === 0 ? '' : ` (${code})`)}`);
      }
      resolve(closeOutcome);
    });
  });
  let stopPromise;
  const stop = () => {
    if (stopPromise) return stopPromise;
    stopped = true;
    stopPromise = (async () => {
      const alreadyClosed = closeOutcome || observedOutcome(child);
      if (!alreadyClosed) {
        try { child.kill('SIGTERM'); } catch { throw cleanupError(); }
      }
      const outcome = alreadyClosed || closeOutcome || await within(closed, stopTimeoutMs, cleanupError());
      if (!cleanupProved(outcome.code, outcome.signal)) throw cleanupError();
    })();
    return stopPromise;
  };
  child.stderr?.on('data', (chunk) => { stderr = appendBounded(stderr, chunk); });
  child.stdout?.on('data', (chunk) => {
    stdoutBytes += Buffer.byteLength(chunk);
    if (stdoutBytes > OUTPUT_LIMIT) return fail('Desktop backend sent too much readiness output.');
    stdout += chunk.toString();
    const newline = stdout.indexOf('\n');
    if (newline < 0) return;
    if (newline !== stdout.length - 1) return fail('Desktop backend sent unexpected output.');
    try {
      const parsed = parseReadyMessage(JSON.parse(stdout.slice(0, -1)));
      if (!parsed) throw new Error();
      readyResolve(parsed);
    } catch { fail('Desktop backend sent an invalid readiness message.'); }
  });
  child.once('error', () => fail('Desktop backend could not be started.'));
  child.stdin?.once?.('error', () => fail('Desktop backend could not receive its startup configuration.'));
  try {
    child.stdin?.end(startupMessage(workspace, token));
    const announced = await within(readiness, remaining(deadline), new Error('Desktop backend did not become ready in time.'));
    if (lifecycleError || closeOutcome || observedOutcome(child)) throw lifecycleError || new Error('Desktop backend exited before health verification.');
    const healthSignal = timeoutSignal(remaining(deadline));
    await within(verifyHealth(announced.host, announced.port, token, fetchImpl, healthSignal), remaining(deadline), new Error('Desktop backend health check timed out.'));
    if (lifecycleError || closeOutcome || observedOutcome(child)) throw lifecycleError || new Error('Desktop backend exited before health verification.');
    started = true;
    return { host: announced.host, port: announced.port, pid: announced.pid, token, stop };
  } catch (error) {
    try { await stop(); } catch (cleanup) { throw cleanup; }
    throw error instanceof Error ? error : new Error('Desktop backend failed to start.');
  }
}

/** @param {string} host @param {number} port @param {string} token @param {typeof fetch} fetchImpl @param {AbortSignal} signal */
export async function verifyHealth(host, port, token, fetchImpl, signal) {
  const response = await fetchImpl(healthUrl(host, port), {
    headers: { 'X-Frisket-Desktop-Token': token }, redirect: 'error', signal,
  });
  if (!response.ok) throw new Error('Desktop backend health check failed.');
}

/** @param {number} deadline */
function remaining(deadline) { return Math.max(1, deadline - Date.now()); }

/** @param {number} ms */
function timeoutSignal(ms) {
  return typeof AbortSignal.timeout === 'function' ? AbortSignal.timeout(ms) : new AbortController().signal;
}

/** @template T @param {Promise<T>} promise @param {number} timeoutMs @param {Error} error */
function within(promise, timeoutMs, error) {
  let timeout;
  const timeoutPromise = new Promise((_, reject) => {
    timeout = setTimeout(() => reject(error), timeoutMs);
    timeout.unref?.();
  });
  return Promise.race([promise, timeoutPromise]).finally(() => clearTimeout(timeout));
}

function cleanupError() { return new Error('Desktop backend cleanup could not be proven.'); }

/** @param {import('node:child_process').ChildProcess} child */
function observedOutcome(child) {
  if (child.exitCode !== null || child.signalCode) {
    return { code: child.exitCode, signal: child.signalCode };
  }
  return null;
}

/** @param {string} value @param {string} token */
function redact(value, token) {
  return value.replaceAll(token, '[redacted]').replace(
    /(token|secret|password|authorization|api[_-]?key)\s*[=:]\s*[^\s]+/gi,
    '$1=[redacted]',
  );
}
