// Networked artifact setup, deliberately separate from the offline app tests.
import { execFile as execFileCallback } from 'node:child_process';
import { cp, mkdir, readdir, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';
import { startBackend } from '../src/backend.mjs';
import { authenticatedHeaders } from '../src/protocol.mjs';
import { prepareRuntime } from '../src/provision.mjs';
import { installedResources } from '../e2e/installed-platform.mjs';

const execFile = promisify(execFileCallback);
const here = path.dirname(fileURLToPath(import.meta.url));
const app = process.env.FRISKET_DESKTOP_APP;
const directResources = process.env.FRISKET_DESKTOP_RESOURCES;
const dataPath = process.env.FRISKET_DESKTOP_PROFILE;
const stageModels = process.argv.slice(2).includes('--stage-models');
if (!dataPath || !path.isAbsolute(dataPath) || Boolean(app) === Boolean(directResources)
  || (app && !path.isAbsolute(app)) || (directResources && !path.isAbsolute(directResources))
  || (stageModels && !directResources)) {
  throw new Error('Set either FRISKET_DESKTOP_APP or FRISKET_DESKTOP_RESOURCES and an absolute FRISKET_DESKTOP_PROFILE path. --stage-models requires FRISKET_DESKTOP_RESOURCES.');
}
const resourcesPath = directResources || installedResources(app);
const runtime = await prepareRuntime({
  resourcesPath, dataPath,
  onProgress: ({ message }) => process.stdout.write(`${message}\n`),
});
if (stageModels) {
  const guard = path.join(resourcesPath, 'python', 'frisket', 'runtime', '_guard.py');
  const modelPrewarm = path.join(here, 'prewarm-models.py');
  try {
    const { stdout } = await execFile(runtime.python, [
      '-I', '-B', guard, String(process.pid), '2', runtime.python, '-I', '-B', modelPrewarm,
      path.join(resourcesPath, 'python'),
    ], {
      env: { ...runtime.env, HF_HUB_DISABLE_PROGRESS_BARS: '1' },
      maxBuffer: 16 * 1024 * 1024,
      timeout: 30 * 60 * 1000,
    });
    process.stdout.write(stdout);
  } catch (error) {
    const detail = String(error.stderr || error.message).slice(-4096);
    throw new Error(`Model cache preparation failed. ${detail}`);
  }
  const bundleCache = path.join(resourcesPath, 'model-cache');
  await rm(bundleCache, { recursive: true, force: true });
  for (const [source, destination] of [
    [path.join(dataPath, 'cache', 'huggingface'), path.join(bundleCache, 'huggingface')],
    [path.join(dataPath, 'cache', 'models', 'rapidocr'), path.join(bundleCache, 'models', 'rapidocr')],
  ]) {
    try {
      await cp(source, destination, {
        recursive: true, preserveTimestamps: true, verbatimSymlinks: true,
      });
    } catch {
      throw new Error('Model cache preparation did not produce the required bundled models.');
    }
  }
} else {
  // This branch runs only in installed-app CI, never during ordinary startup.
  // Bundled models must seed themselves again on first launch. Transcription
  // weights must be downloaded through the same durable setup operation as the UI.
  const bundled = await readdir(path.join(resourcesPath, 'model-cache', 'huggingface'));
  if (bundled.some((name) => /parakeet|whisper/i.test(name))) {
    throw new Error('Transcription weights must not be bundled in the app.');
  }
  await Promise.all([
    rm(path.join(dataPath, 'cache', 'huggingface'), { recursive: true, force: true }),
    rm(path.join(dataPath, 'cache', 'models', 'rapidocr'), { recursive: true, force: true }),
  ]);
  const workspace = path.join(dataPath, 'workspace');
  await mkdir(workspace, { recursive: true });
  let backendFailure;
  const backend = await startBackend({
    runtime, resourcesPath, workspace,
    onFailure: (error) => { backendFailure = error; },
  });
  try {
    const request = async (endpoint, options = {}) => {
      const response = await fetch(`http://${backend.host}:${backend.port}${endpoint}`, {
        ...options,
        headers: authenticatedHeaders({ 'Content-Type': 'application/json' }, backend.token),
        signal: AbortSignal.timeout(30_000),
      });
      if (!response.ok) throw new Error(`Model preparation returned HTTP ${response.status}.`);
      return response.json();
    };
    const { stdout: whisperRef } = await execFile(runtime.python, [
      '-I', '-B', path.join(here, 'prewarm-models.py'),
      path.join(resourcesPath, 'python'), '--whisper-ref',
    ], { env: runtime.env, timeout: 30_000 });
    for (const [label, endpoint, payload] of [
      ['Parakeet', '/api/providers/models/setup', {
        setup_ref: 'engine-setup:parakeet-tdt.local-onnx@1',
      }],
      ['Whisper base', '/api/providers/models/pull', {
        ref: whisperRef.trim(),
      }],
    ]) {
      process.stdout.write(`Downloading ${label} into the test profile through model setup…\n`);
      let { pull } = await request(endpoint, {
        method: 'POST', body: JSON.stringify(payload),
      });
      const deadline = Date.now() + 30 * 60 * 1000;
      while (pull.status !== 'done') {
        if (backendFailure) throw backendFailure;
        if (['failed', 'cancelled'].includes(pull.status)) {
          throw new Error(`${label} setup ${pull.status}: ${pull.error?.message || 'no detail'}`);
        }
        if (Date.now() >= deadline) throw new Error(`${label} setup timed out.`);
        await delay(1_000);
        pull = await request(`/api/providers/models/pulls/${pull.id}`);
      }
    }
    process.stdout.write('Transcription weights downloaded; native checks will run offline.\n');
  } finally {
    await backend.stop();
  }
  await writeFile(path.join(dataPath, '.frisket-prewarm-runtime-python'), `${runtime.python}\n`, {
    mode: 0o600,
  });
}
// Keep only downloaded interpreters/packages/browsers, not a completed venv.
await rm(path.join(dataPath, 'runtimes'), { recursive: true, force: true });
