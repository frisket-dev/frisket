// Networked artifact setup, deliberately separate from the offline app tests.
import { execFile as execFileCallback } from 'node:child_process';
import { cp, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { prepareRuntime } from '../src/provision.mjs';

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
const resourcesPath = directResources || path.join(app, 'Contents', 'Resources');
const runtime = await prepareRuntime({
  resourcesPath, dataPath,
  onProgress: ({ message }) => process.stdout.write(`${message}\n`),
});
const guard = path.join(resourcesPath, 'python', 'frisket', 'runtime', '_guard.py');
const modelPrewarm = path.join(here, 'prewarm-models.py');
try {
  const { stdout } = await execFile(runtime.python, [
    '-I', guard, String(process.pid), '2', runtime.python, '-I', modelPrewarm,
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
if (stageModels) {
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
}
// Keep only downloaded interpreters/packages/browsers, not a completed venv.
await rm(path.join(dataPath, 'runtimes'), { recursive: true, force: true });
