// Networked artifact setup, deliberately separate from the offline app tests.
import { execFile as execFileCallback } from 'node:child_process';
import { rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { prepareRuntime } from '../src/provision.mjs';

const execFile = promisify(execFileCallback);
const here = path.dirname(fileURLToPath(import.meta.url));
const app = process.env.FRISKET_DESKTOP_APP;
const dataPath = process.env.FRISKET_DESKTOP_PROFILE;
if (!app || !dataPath || !path.isAbsolute(app) || !path.isAbsolute(dataPath)) {
  throw new Error('Set absolute FRISKET_DESKTOP_APP and FRISKET_DESKTOP_PROFILE paths.');
}
const resourcesPath = path.join(app, 'Contents', 'Resources');
const runtime = await prepareRuntime({
  resourcesPath, dataPath,
  onProgress: ({ message }) => process.stdout.write(`${message}\n`),
});
const guard = path.join(resourcesPath, 'python', 'frisket', 'runtime', '_guard.py');
const modelPrewarm = path.join(here, 'prewarm-asr.py');
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
  throw new Error(`ASR model cache preparation failed. ${detail}`);
}
// Keep only downloaded interpreters/packages/browsers, not a completed venv.
await rm(path.join(dataPath, 'runtimes'), { recursive: true, force: true });
