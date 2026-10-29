// Networked first-launch proof. Run before prewarming; no credentials or real projects.
import { _electron } from '@playwright/test';
import { access, readFile, readdir, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { installedExecutable } from '../e2e/installed-platform.mjs';

const profile = process.env.FRISKET_DESKTOP_PROFILE && `${process.env.FRISKET_DESKTOP_PROFILE}-cold-start`;
const app = process.env.FRISKET_DESKTOP_APP;
if (!profile || !path.isAbsolute(profile) || !app || !path.isAbsolute(app)) throw new Error('Absolute installed app and profile paths are required.');
try {
  await access(profile);
  throw new Error('Cold-start proof requires a new profile, not a prewarmed one.');
} catch (error) { if (error.code !== 'ENOENT') throw error; }
const env = { ...process.env };
for (const key of ['UV_CACHE_DIR', 'UV_OFFLINE', 'UV_PYTHON_INSTALL_DIR', 'PLAYWRIGHT_BROWSERS_PATH']) delete env[key];
const started = Date.now();
const electron = await _electron.launch({ executablePath: installedExecutable(app), args: [`--user-data-dir=${profile}`], chromiumSandbox: true, timeout: 60000, env });
electron.process().stderr.on('data', chunk => process.stderr.write(chunk));
await electron.evaluate(({ dialog }) => {
  const original = dialog.showMessageBox.bind(dialog);
  dialog.showMessageBox = async (...args) => {
    const options = args.at(-1);
    if (options.message === 'Check for updates automatically?') return { response: options.buttons.indexOf('Only when I ask') };
    if (options.type === 'error') {
      process.stderr.write(`Cold-start failure (no retry): ${options.message} ${options.detail}\n`);
      return { response: options.buttons.indexOf('Quit') };
    }
    return original(...args);
  };
});
let page;
try {
  page = await electron.firstWindow();
  await page.waitForURL('frisket://app/**', { timeout: 15 * 60000 });
  await page.getByTestId('home-screen').waitFor({ timeout: 30000 });
  if (process.env.FRISKET_MODEL_RUNTIME_DIR) {
    try {
      await access(path.join(process.env.FRISKET_MODEL_RUNTIME_DIR, 'speech-v1'));
      throw new Error('Fresh first launch unexpectedly created a speech runtime.');
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  const browserCaches = await readdir(path.join(profile, 'cache', 'playwright'));
  for (const version of browserCaches) {
    const contents = await readdir(path.join(profile, 'cache', 'playwright', version));
    if (contents.length) throw new Error('Fresh first launch unexpectedly installed browser artifacts.');
  }
  console.log(`Cold first launch succeeded without Retry in ${((Date.now() - started) / 1000).toFixed(1)}s.`);
} catch (error) {
  await mkdir('desktop/test-results', { recursive: true });
  await page?.screenshot({ path: 'desktop/test-results/cold-start-failure.png' }).catch(() => {});
  throw error;
} finally {
  console.log(await readFile(path.join(profile, 'startup-diagnostics.txt'), 'utf8').catch(() => 'No startup diagnostics file; startup did not finish.'));
  await electron.close().catch(() => {});
}
