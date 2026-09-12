import { createHash } from 'node:crypto';
import { constants as fsConstants, promises as fs } from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { CleanupError } from './errors.mjs';

const MARKER = '.frisket-runtime-ready.json';
const TAIL_LIMIT = 8_192;
const GUARD_GRACE_SECONDS = '2';
const FORCE_CANCEL_MS = 5_000;

/**
 * Prepare the private Python runtime shipped with this application.
 *
 * The caller must hold the application-level provisioning lock. A directory
 * without the completion marker is deliberately treated as repairable; a
 * marked directory is immutable from this module's point of view.
 *
 * @param {{resourcesPath: string, dataPath: string, onProgress?: (status: string) => void, signal?: AbortSignal}} options
 * @returns {Promise<{python: string, env: Record<string, string>}>}
 */
export async function prepareRuntime({ resourcesPath, dataPath, onProgress = () => {}, signal }) {
  throwIfAborted(signal);
  const resources = path.resolve(resourcesPath);
  const data = path.resolve(dataPath);
  const manifest = await readManifest(path.join(resources, 'runtime.json'));
  const requirements = path.join(resources, 'requirements.txt');
  await verifyRequirements(requirements, manifest.requirementsSha256);
  await verifyResources(resources);
  assertSupportedPlatform();

  const environmentId = runtimeId(manifest);
  const environmentPath = path.join(data, 'runtimes', environmentId);
  const python = pythonPath(environmentPath);
  const env = backendEnvironment({ resources, data, environmentPath, manifest });
  const markerPath = path.join(environmentPath, MARKER);

  const ready = await readReadyMarker(markerPath, manifest, environmentId);
  if (ready) {
    await assertFile(python, 'private Python');
    onProgress('Private Python runtime is ready.');
    return { python, env };
  }

  const uv = path.join(resources, 'bin', 'uv');
  const bootstrap = path.join(resources, 'python', 'frisket', 'runtime', '_bootstrap.py');
  const guard = path.join(resources, 'python', 'frisket', 'runtime', '_guard.py');
  const wasIncomplete = await exists(environmentPath);

  await fs.mkdir(path.join(data, 'runtimes'), { recursive: true });
  await fs.mkdir(path.join(data, 'python'), { recursive: true });
  await fs.mkdir(path.join(data, 'cache', 'playwright', manifest.playwrightVersion), { recursive: true });

  onProgress('Installing the private Python interpreter…');
  await run(uv, ['python', 'install', manifest.pythonVersion, '--managed-python', '--no-bin', '--no-config'], {
    label: 'installing private Python', env, signal,
  });

  onProgress(wasIncomplete ? 'Repairing the private Python environment…' : 'Creating the private Python environment…');
  const venvArgs = ['venv', environmentPath, '--python', manifest.pythonVersion, '--managed-python', '--no-config'];
  if (wasIncomplete) venvArgs.push('--clear');
  await run(uv, venvArgs, { label: 'creating private Python environment', env, signal });
  await assertFile(python, 'private Python');

  // Once a venv exists, the bundled guardian owns each child process group.
  const guarded = (command, args, label) => run(python, ['-I', guard, String(process.pid), GUARD_GRACE_SECONDS, command, ...args], {
    label, env, signal,
  });
  onProgress('Installing private Python dependencies…');
  await guarded(uv, ['pip', 'sync', '--python', python, '--require-hashes', requirements, '--no-config'], 'installing private Python dependencies');

  onProgress('Installing the bundled Chromium browser…');
  await guarded(python, ['-m', 'playwright', 'install', 'chromium'], 'installing bundled Chromium');

  onProgress('Checking the private Python runtime…');
  await guarded(python, ['-I', bootstrap, 'runtime-info'], 'checking private Python runtime');

  await writeReadyMarker(markerPath, manifest, environmentId);
  onProgress('Private Python runtime is ready.');
  return { python, env };
}

function pythonPath(environmentPath) {
  return path.join(environmentPath, 'bin', 'python');
}

function runtimeId(manifest) {
  const identity = [manifest.pythonVersion, manifest.requirementsSha256, manifest.uvVersion, process.platform, process.arch].join('\0');
  return `runtime-${createHash('sha256').update(identity).digest('hex').slice(0, 24)}`;
}

function assertSupportedPlatform() {
  if (process.platform === 'darwin' && process.arch !== 'arm64') {
    throw new Error('Frisket desktop requires Apple Silicon on macOS.');
  }
  if (process.platform !== 'darwin' && process.platform !== 'linux') {
    throw new Error(`Unsupported desktop runtime platform: ${process.platform}/${process.arch}`);
  }
}

async function readManifest(filename) {
  let value;
  try {
    value = JSON.parse(await fs.readFile(filename, 'utf8'));
  } catch {
    throw new Error('Runtime manifest is missing or invalid.');
  }
  if (!value || value.schema !== 1 || !isVersion(value.pythonVersion) || !isVersion(value.uvVersion)
    || !isVersion(value.playwrightVersion) || !/^[a-f0-9]{64}$/i.test(value.requirementsSha256 ?? '')) {
    throw new Error('Runtime manifest has an unsupported format.');
  }
  return { ...value, requirementsSha256: value.requirementsSha256.toLowerCase() };
}

function isVersion(value) {
  return typeof value === 'string' && /^\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(value);
}

async function verifyRequirements(filename, expected) {
  let bytes;
  try {
    bytes = await fs.readFile(filename);
  } catch {
    throw new Error('Bundled runtime requirements are missing.');
  }
  const actual = createHash('sha256').update(bytes).digest('hex');
  if (actual !== expected.toLowerCase()) {
    throw new Error('Bundled runtime requirements failed integrity verification.');
  }
}

async function verifyResources(resources) {
  const required = [
    path.join(resources, 'bin', 'uv'),
    path.join(resources, 'bin', 'ffmpeg'),
    path.join(resources, 'bin', 'ffprobe'),
    path.join(resources, 'bin', 'deno'),
    path.join(resources, 'python', 'frisket', 'runtime', '_bootstrap.py'),
    path.join(resources, 'python', 'frisket', 'runtime', '_guard.py'),
  ];
  await Promise.all(required.map((filename) => assertFile(filename, 'bundled runtime resource')));
}

async function assertFile(filename, description) {
  try {
    await fs.access(filename, fsConstants.R_OK);
  } catch {
    throw new Error(`${description} is missing.`);
  }
}

async function readReadyMarker(filename, manifest, environmentId) {
  if (!await exists(filename)) return false;
  let marker;
  try {
    marker = JSON.parse(await fs.readFile(filename, 'utf8'));
  } catch {
    throw new Error('Private runtime completion marker is invalid; refusing to modify it.');
  }
  if (marker.environmentId !== environmentId || marker.pythonVersion !== manifest.pythonVersion
    || marker.requirementsSha256 !== manifest.requirementsSha256 || marker.uvVersion !== manifest.uvVersion
    || marker.playwrightVersion !== manifest.playwrightVersion) {
    throw new Error('Private runtime completion marker does not match the bundled runtime; refusing to modify it.');
  }
  return true;
}

async function writeReadyMarker(filename, manifest, environmentId) {
  const marker = JSON.stringify({
    environmentId,
    pythonVersion: manifest.pythonVersion,
    requirementsSha256: manifest.requirementsSha256,
    uvVersion: manifest.uvVersion,
    playwrightVersion: manifest.playwrightVersion,
  });
  const temporary = `${filename}.${process.pid}.${Date.now()}.tmp`;
  await fs.writeFile(temporary, marker, { mode: 0o600 });
  await fs.rename(temporary, filename);
}

function backendEnvironment({ resources, data, environmentPath, manifest }) {
  const resourcesBin = path.join(resources, 'bin');
  const cache = path.join(data, 'cache');
  const inheritedCache = process.env.UV_CACHE_DIR;
  const env = cleanChildEnvironment();
  const inheritedPath = process.env.PATH ?? '';
  env.PATH = [resourcesBin, path.dirname(pythonPath(environmentPath)), inheritedPath].filter(Boolean).join(path.delimiter);
  env.UV_CACHE_DIR = inheritedCache || path.join(cache, 'uv');
  env.UV_PYTHON_INSTALL_DIR = path.join(data, 'python');
  if (process.env.UV_OFFLINE !== undefined) env.UV_OFFLINE = process.env.UV_OFFLINE;
  env.PLAYWRIGHT_BROWSERS_PATH = path.join(cache, 'playwright', manifest.playwrightVersion);
  env.FRISKET_FFPROBE_PATH = path.join(resourcesBin, 'ffprobe');
  env.FRISKET_MODEL_CACHE_DIR = path.join(cache, 'models');
  env.HF_HUB_CACHE = path.join(cache, 'huggingface');
  env.FASTEMBED_CACHE_PATH = path.join(cache, 'fastembed');
  env.XDG_CACHE_HOME = cache;
  return env;
}

function cleanChildEnvironment() {
  const env = {};
  for (const name of ['HOME', 'TMPDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR']) {
    if (process.env[name] !== undefined) env[name] = process.env[name];
  }
  return env;
}

async function exists(filename) {
  try {
    await fs.access(filename);
    return true;
  } catch {
    return false;
  }
}

function throwIfAborted(signal) {
  if (signal?.aborted) throw abortError();
}

function abortError() {
  const error = new Error('Private runtime provisioning was cancelled.');
  error.name = 'AbortError';
  return error;
}

function redact(value) {
  return value
    .replace(/((?:x-)?(?:access[-_])?(?:token|secret|password|authorization|api[_-]?key))\s*[=:][^\r\n]*/gi, '$1=[redacted]')
    .replace(/:\/\/[^\s/@]+@/g, '://[redacted]@');
}

function appendTail(current, chunk) {
  const next = `${current}${chunk}`;
  return next.length > TAIL_LIMIT ? next.slice(-TAIL_LIMIT) : next;
}

function run(command, args, { label, env, signal }) {
  throwIfAborted(signal);
  return new Promise((resolve, reject) => {
    let output = '';
    let aborted = false;
    let forcedCleanup = false;
    let settled = false;
    let forceTimer;
    const child = spawn(command, args, { env, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    const clearForceTimer = () => {
      if (forceTimer) clearTimeout(forceTimer);
      forceTimer = undefined;
    };
    const cancel = () => {
      aborted = true;
      if (child.pid) {
        try {
          process.kill(-child.pid, 'SIGTERM');
        } catch (error) {
          if (error.code !== 'ESRCH') {
            rejectOnce(new CleanupError('Private runtime cancellation could not prove process cleanup.'));
          }
          return;
        }
        forceTimer = setTimeout(() => {
          forcedCleanup = true;
          try { process.kill(-child.pid, 'SIGKILL'); } catch {}
        }, FORCE_CANCEL_MS).unref();
      }
    };
    const rejectOnce = (error) => {
      if (settled) return;
      settled = true;
      clearForceTimer();
      signal?.removeEventListener('abort', cancel);
      reject(error);
    };
    child.stdout.on('data', (chunk) => { output = appendTail(output, chunk); });
    child.stderr.on('data', (chunk) => { output = appendTail(output, chunk); });
    child.once('error', (error) => rejectOnce(new Error(`Private runtime failed while ${label}: ${redact(error.message)}`)));
    child.once('close', (code, childSignal) => {
      if (settled) return;
      settled = true;
      clearForceTimer();
      signal?.removeEventListener('abort', cancel);
      if (aborted || signal?.aborted) {
        if (forcedCleanup) return reject(new CleanupError('Private runtime cancellation could not prove process cleanup.'));
        return reject(abortError());
      }
      if (code === 0) return resolve();
      const tail = redact(output.trim());
      const detail = tail ? ` ${tail}` : childSignal ? ` (${childSignal})` : '';
      reject(new Error(`Private runtime failed while ${label}.${detail}`));
    });
    signal?.addEventListener('abort', cancel, { once: true });
    if (signal?.aborted) cancel();
  });
}
