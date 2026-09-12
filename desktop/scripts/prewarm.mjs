// Networked artifact setup, deliberately separate from the offline app tests.
import { rm } from 'node:fs/promises';
import path from 'node:path';
import { prepareRuntime } from '../src/provision.mjs';

const app = process.env.FRISKET_DESKTOP_APP;
const dataPath = process.env.FRISKET_DESKTOP_PROFILE;
if (!app || !dataPath || !path.isAbsolute(app) || !path.isAbsolute(dataPath)) {
  throw new Error('Set absolute FRISKET_DESKTOP_APP and FRISKET_DESKTOP_PROFILE paths.');
}
await prepareRuntime({
  resourcesPath: path.join(app, 'Contents', 'Resources'), dataPath,
  onProgress: ({ message }) => process.stdout.write(`${message}\n`),
});
// Keep only downloaded interpreters/packages/browsers, not a completed venv.
await rm(path.join(dataPath, 'runtimes'), { recursive: true, force: true });
