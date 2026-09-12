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
  if (
    ready.schema !== 1 || ready.type !== 'ready' || ready.host !== '127.0.0.1' ||
    !Number.isInteger(ready.port) || ready.port < 1 || ready.port > 65535 ||
    !Number.isInteger(ready.pid) || ready.pid < 1
  ) return null;
  return /** @type {{ schema: 1, type: 'ready', host: '127.0.0.1', port: number, pid: number }} */ (ready);
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
 * Start the private backend under the bundled guardian.  The random token is
 * kept in this closure and is never placed in argv or the environment.
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

  let stopped = false;
  let ready = false;
  let failureReported = false;
  let stderr = '';
  let stdout = '';
  let readyResolve;
  let readyReject;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let timeout;
  const readiness = new Promise((resolve, reject) => {
    readyResolve = resolve;
    readyReject = reject;
  });
  const fail = (message) => {
    const error = message instanceof Error ? message : new Error(message);
    if (!ready) readyReject(error);
    if (ready && !stopped && !failureReported) {
      failureReported = true;
      onFailure(error);
    }
  };
  const finishReady = (message) => {
    const parsed = parseReadyMessage(message);
    if (!parsed || ready) return fail('Desktop backend sent an invalid readiness message.');
    ready = true;
    clearTimeout(timeout);
    readyResolve(parsed);
  };
  child.stderr?.on('data', (chunk) => { stderr = appendBounded(stderr, chunk); });
  child.stdout?.on('data', (chunk) => {
    stdout = appendBounded(stdout, chunk);
    const lines = stdout.split('\n');
    // A readiness protocol has exactly one NDJSON line. Any additional
    // complete line is evidence the child is not speaking this protocol.
    if (lines.length > 2 || (lines.length === 2 && lines[1] !== '')) {
      fail('Desktop backend sent unexpected output.');
      return;
    }
    if (lines.length === 2) {
      try { finishReady(JSON.parse(lines[0])); } catch { fail('Desktop backend sent invalid readiness JSON.'); }
    }
  });
  child.once('error', () => fail('Desktop backend could not be started.'));
  child.once('close', (code, signal) => {
    clearTimeout(timeout);
    if (!stopped) {
      const detail = stderr.trim() ? ` ${redact(stderr.trim(), token)}` : '';
      fail(`Desktop backend exited unexpectedly.${detail || (signal ? ` (${signal})` : code === 0 ? '' : ` (${code})`)}`);
    }
  });
  timeout = setTimeout(() => fail('Desktop backend did not become ready in time.'), readyTimeoutMs);
  timeout.unref?.();
  child.stdin?.end(startupMessage(workspace, token));

  let announced;
  try {
    announced = await readiness;
    await verifyHealth(announced.host, announced.port, token, fetchImpl);
  } catch (error) {
    stopped = true;
    try { child.kill('SIGTERM'); } catch {}
    throw error instanceof Error ? error : new Error('Desktop backend failed to start.');
  }

  let stopPromise;
  const stop = () => {
    if (stopPromise) return stopPromise;
    stopped = true;
    stopPromise = waitForClose(child, stopTimeoutMs);
    try { child.kill('SIGTERM'); } catch (error) {
      stopPromise = Promise.reject(new Error('Desktop backend cleanup could not be proven.'));
    }
    return stopPromise;
  };
  return { host: announced.host, port: announced.port, pid: announced.pid, token, stop };
}

/** @param {string} host @param {number} port @param {string} token @param {typeof fetch} fetchImpl */
export async function verifyHealth(host, port, token, fetchImpl) {
  const response = await fetchImpl(healthUrl(host, port), {
    headers: { 'X-Frisket-Desktop-Token': token }, redirect: 'error',
  });
  if (!response.ok) throw new Error('Desktop backend health check failed.');
}

/** @param {import('node:child_process').ChildProcess} child @param {number} timeoutMs */
function waitForClose(child, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (child.exitCode !== null) return resolve();
    const timeout = setTimeout(() => reject(new Error('Desktop backend cleanup could not be proven.')), timeoutMs);
    timeout.unref?.();
    child.once('close', () => { clearTimeout(timeout); resolve(); });
    child.once('error', () => { clearTimeout(timeout); reject(new Error('Desktop backend cleanup could not be proven.')); });
  });
}

/** @param {string} value @param {string} token */
function redact(value, token) {
  return value.replaceAll(token, '[redacted]').replace(
    /(token|secret|password|authorization|api[_-]?key)\s*[=:]\s*[^\s]+/gi,
    '$1=[redacted]',
  );
}
