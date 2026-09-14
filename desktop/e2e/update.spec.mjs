import { test, expect, _electron } from '@playwright/test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import {
  alivePids, assertInstalledPlatform, descendants, installedExecutable, installedResources, listeningPorts, runningExecutable,
} from './installed-platform.mjs';
import { serveUpdateFeed } from './update-feed.mjs';

const require = createRequire(import.meta.url);
const { extractFile, uncache } = require('@electron/asar');
const appPath = process.env.FRISKET_DESKTOP_APP;
const profile = process.env.FRISKET_DESKTOP_PROFILE;
const targetDist = process.env.FRISKET_DESKTOP_UPDATE_TARGET_DIST;
const targetVersion = process.env.FRISKET_DESKTOP_UPDATE_TARGET_VERSION;

async function launch(feed, testInfo) {
  const electron = await _electron.launch({
    executablePath: installedExecutable(appPath),
    args: [`--user-data-dir=${profile}`],
    chromiumSandbox: true,
    timeout: 60_000,
    env: { ...process.env, UV_OFFLINE: '1' },
  });
  let stderr = '';
  electron.process().stderr.on('data', (chunk) => { stderr = `${stderr}${chunk}`.slice(-16_384); });
  try {
    await electron.evaluate(async ({ app, dialog }, origin) => {
      const proof = globalThis.__frisketUpdateProof = { downloaded: null, errors: [], prompts: [], restart: false };
      const showMessageBox = dialog.showMessageBox.bind(dialog);
      dialog.showMessageBox = async (...args) => {
        const options = args.at(-1);
        if (options.buttons?.includes('Restart to update') && options.buttons.includes('Later')) {
          proof.prompts.push({ message: options.message, buttons: options.buttons });
          return { response: options.buttons.indexOf(proof.restart ? 'Restart to update' : 'Later'), checkboxChecked: false };
        }
        if (options.type === 'error') {
          proof.errors.push(`${options.message} ${options.detail ?? ''}`);
          return { response: Math.max(0, options.buttons?.indexOf('Quit') ?? 0), checkboxChecked: false };
        }
        // Informational native dialogs cannot wait for a CI operator.
        if (options.buttons?.length === 1) return { response: 0, checkboxChecked: false };
        return showMessageBox(...args);
      };
      if (origin) {
        const { createRequire } = process.getBuiltinModule('module');
        const { autoUpdater } = createRequire(`${app.getAppPath()}/package.json`)('electron-updater');
        // Privileged harness setup, before the normal 30-second background check.
        // No production feed override, download mock, or installer replacement.
        autoUpdater.setFeedURL({ provider: 'generic', url: origin, channel: 'latest' });
        autoUpdater.allowPrerelease = true;
        autoUpdater.disableDifferentialDownload = true;
        // Let Playwright relaunch with the same explicit isolated profile after
        // the native installer finishes; normal releases launch automatically.
        autoUpdater.autoRunAppAfterInstall = false;
        autoUpdater.on('update-downloaded', (info) => {
          proof.downloaded = info.version;
          proof.installer = info.downloadedFile;
        });
        autoUpdater.on('error', (error) => proof.errors.push(error.message));
      }
    }, feed?.origin);
    const page = await electron.firstWindow();
    const rendererErrors = [];
    page.on('pageerror', (error) => rendererErrors.push(error.message));
    await page.waitForURL('frisket://app/**', { waitUntil: 'commit', timeout: 300_000 });
    await expect(page.getByTestId('home-screen')).toBeVisible();
    const disclosure = page.getByTestId('product-telemetry-disclosure');
    if (await disclosure.isVisible()) {
      await disclosure.getByRole('checkbox').uncheck();
      await disclosure.getByRole('button', { name: 'Continue' }).click();
    }
    expect(await electron.evaluate(({ app }) => app.getPath('userData'))).toBe(profile);
    return { electron, page, rendererErrors };
  } catch (error) {
    await testInfo.attach('update-startup-stderr', { body: stderr, contentType: 'text/plain' });
    const state = await electron.evaluate(({ BrowserWindow }) => ({
      proof: globalThis.__frisketUpdateProof,
      windows: BrowserWindow.getAllWindows().map((window) => ({
        url: window.webContents.getURL(), loading: window.webContents.isLoading(),
      })),
    })).catch(() => null);
    await testInfo.attach('update-startup-state', { body: JSON.stringify(state), contentType: 'application/json' });
    await electron.windows()[0]?.screenshot({ path: testInfo.outputPath('update-startup-failure.png') }).catch(() => {});
    await electron.close().catch(() => {});
    throw error;
  }
}

async function updateMenu(electron) {
  return electron.evaluate(({ Menu }) => {
    const find = (items) => {
      for (const item of items) {
        if (item.id === 'desktop-update') return item;
        const nested = item.submenu && find(item.submenu.items);
        if (nested) return nested;
      }
    };
    const item = find(Menu.getApplicationMenu()?.items ?? []);
    if (!item) throw new Error('Installed app has no desktop-update menu item.');
    if (!item.enabled) throw new Error(`Update menu is disabled: ${item.label}`);
    const label = item.label;
    item.click();
    return label;
  });
}

async function proofState(electron) {
  const state = await electron.evaluate(() => globalThis.__frisketUpdateProof);
  expect(state.errors).toEqual([]);
  return state;
}

async function sheet(page, projectId, sheetId) {
  return page.evaluate(async ({ projectId, sheetId }) => {
    const response = await fetch(`/api/projects/${projectId}/sheets/${sheetId}/data?offset=0&limit=20`);
    if (!response.ok) throw new Error(`Saved sheet read failed: ${response.status}`);
    return response.json();
  }, { projectId, sheetId });
}

function extracted(data) {
  const column = data.columns.find((column) => column.name === 'extracted');
  return column ? data.rows[0].cells[String(column.id)] : null;
}

async function savedProject(page) {
  await page.getByTestId('home-new-project').click();
  await page.getByTestId('new-project-name').fill('Desktop signed update proof');
  await page.getByTestId('create-project').click();
  await page.waitForURL(/\/p\//);
  const projectId = new URL(page.url()).pathname.split('/')[2];
  if (!await page.getByTestId('import-csv-input').count()) {
    await page.getByTestId('ribbon-tab-data').click();
    await page.getByTestId('ribbon-command-import').click();
  }
  const uploaded = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/import/csv'));
  await page.getByTestId('import-csv-input').setInputFiles({
    name: 'update-proof.csv', mimeType: 'text/csv',
    buffer: Buffer.from('note\n"saved contract worth $4,200"\n'),
  });
  await expect(page.getByTestId('import-csv-preview')).toBeVisible();
  await page.getByTestId('import-csv-confirm').click();
  const response = await uploaded;
  expect(response.ok()).toBeTruthy();
  const { sheet_id: sheetId } = await response.json();
  expect(sheetId).toBeTruthy();
  const ribbon = page.getByTestId('act-ribbon');
  const tile = page.getByTestId('ribbon-action-map.regex_extract');
  await expect(ribbon).toBeVisible();
  await expect(async () => {
    const tabs = ribbon.getByRole('tab');
    for (let index = 0; index < await tabs.count() && !await tile.isVisible(); index++) await tabs.nth(index).click();
    expect(await tile.isVisible()).toBeTruthy();
  }).toPass({ timeout: 20_000, intervals: [100, 250, 500] });
  await tile.click();
  await page.getByTestId('field-input_columns').click();
  await page.getByTestId('field-input_columns-menu').getByRole('option').filter({ hasText: 'note' }).click();
  await page.keyboard.press('Escape');
  await page.getByTestId('field-pattern').fill('\\$[0-9,]+');
  await page.getByTestId('generated-action-run').click();
  await expect.poll(async () => extracted(await sheet(page, projectId, sheetId)), { timeout: 60_000 }).toBe('$4,200');
  return { projectId, sheetId };
}

test('signed installed baseline updates through its native updater and preserves the workspace', async ({}, testInfo) => {
  assertInstalledPlatform();
  for (const value of [appPath, profile, targetDist]) {
    expect(typeof value).toBe('string');
    expect(path.isAbsolute(value)).toBeTruthy();
  }
  expect(typeof targetVersion).toBe('string');
  expect(targetVersion).not.toBe('0.0.0');
  const feed = await serveUpdateFeed(targetDist);
  expect(feed.metadata.version).toBe(targetVersion);
  let running;
  try {
    const baseline = await launch(feed, testInfo);
    running = baseline.electron;
    expect(await running.evaluate(({ app }) => app.getVersion())).toBe('0.0.0');
    expect(await updateMenu(running)).toMatch(/Check for Updates/);
    const saved = await savedProject(baseline.page); // Starts the actual private worker.
    const sentinel = path.join(profile, 'cache', 'signed-update-proof.txt');
    const sentinelValue = 'Preserve the original profile and model cache across native updates.\n';
    await mkdir(path.dirname(sentinel), { recursive: true });
    await writeFile(sentinel, sentinelValue);
    await expect.poll(async () => (await proofState(running)).downloaded, { timeout: 180_000 }).toBe(targetVersion);
    const installer = (await proofState(running)).installer;
    if (process.platform === 'win32') expect(path.isAbsolute(installer)).toBeTruthy();
    expect(feed.requests.some((request) => request.method === 'GET' && feed.artifacts.includes(request.name))).toBeTruthy();
    expect(await updateMenu(running)).toMatch(/Restart to update/);
    await expect.poll(async () => (await proofState(running)).prompts.length).toBe(1);
    expect(running.process().exitCode).toBeNull();
    expect(extracted(await sheet(baseline.page, saved.projectId, saved.sheetId))).toBe('$4,200');
    expect(await readFile(sentinel, 'utf8')).toBe(sentinelValue);
    expect(baseline.rendererErrors).toEqual([]);
    const child = running.process();
    const oldPids = await descendants(child.pid);
    const oldPorts = await listeningPorts(oldPids);
    expect(oldPids.length).toBeGreaterThan(1);
    expect(oldPorts.length).toBeGreaterThan(0);
    await running.evaluate(() => { globalThis.__frisketUpdateProof.restart = true; });
    await updateMenu(running);
    // Mac stages the downloaded ZIP through Squirrel only after restart is
    // requested. Wait for actual replacement, not just update-downloaded.
    await expect.poll(() => child.exitCode, { timeout: 90_000 }).toBe(0);
    running = undefined;
    await expect.poll(() => alivePids(oldPids), { timeout: 30_000 }).toEqual([]);
    await expect.poll(async () => Promise.all(oldPorts.map(async (port) => {
      try { await fetch(`http://127.0.0.1:${port}/api/health`, { signal: AbortSignal.timeout(1_000) }); return true; }
      catch { return false; }
    })), { timeout: 30_000 }).toEqual(oldPorts.map(() => false));
    const installedArchive = path.join(installedResources(appPath), 'app.asar');
    await expect.poll(() => {
      try {
        uncache(installedArchive);
        return JSON.parse(extractFile(installedArchive, 'package.json').toString()).version;
      }
      catch { return null; } // Installer can replace the archive between reads.
    }, { timeout: 120_000 }).toBe(targetVersion);
    // NSIS writes app.asar before it finishes the rest of the installation.
    // Wait for the actual downloaded installer to exit before opening the app.
    if (process.platform === 'win32') {
      await expect.poll(() => runningExecutable(installer), { timeout: 120_000 }).toEqual([]);
    }
    const updated = await launch(feed, testInfo);
    running = updated.electron;
    expect(await running.evaluate(({ app }) => app.getVersion())).toBe(targetVersion);
    await updated.page.getByTestId(`project-${saved.projectId}`).click();
    await expect(updated.page.getByTestId('grid')).toBeVisible();
    expect(extracted(await sheet(updated.page, saved.projectId, saved.sheetId))).toBe('$4,200');
    expect(await readFile(sentinel, 'utf8')).toBe(sentinelValue);
    expect(updated.rendererErrors).toEqual([]);
    await updated.page.screenshot({ path: testInfo.outputPath('updated-reopened-project.png') });
    await testInfo.attach('installed-update-evidence', {
      body: JSON.stringify({ baseline: '0.0.0', target: targetVersion, saved, oldPids, oldPorts, requests: feed.requests }, null, 2),
      contentType: 'application/json',
    });
    const finalPids = await descendants(running.process().pid);
    await running.close();
    running = undefined;
    await expect.poll(() => alivePids(finalPids), { timeout: 20_000 }).toEqual([]);
  } finally {
    await running?.close().catch(() => {});
    await feed.close();
  }
});
