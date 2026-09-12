import { test, expect, _electron } from '@playwright/test';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, mkdir } from 'node:fs/promises';
import path from 'node:path';

const exec = promisify(execFile);
const appPath = process.env.FRISKET_DESKTOP_APP;
const profile = process.env.FRISKET_DESKTOP_PROFILE;

async function launch(testInfo) {
  const electron = await _electron.launch({
    executablePath: path.join(appPath, 'Contents/MacOS/Frisket Desktop'),
    args: [`--user-data-dir=${profile}`],
    chromiumSandbox: true,
    timeout: 60_000,
    env: { ...process.env, UV_OFFLINE: '1' },
  });
  let stderr = '';
  electron.process().stderr.on('data', (chunk) => {
    stderr = `${stderr}${chunk}`.slice(-16_384);
  });
  // A native error dialog must fail CI rather than wait for a human to click Quit.
  await electron.evaluate(({ dialog }) => {
    const showMessageBox = dialog.showMessageBox.bind(dialog);
    dialog.showMessageBox = async (...args) => {
      const options = args.at(-1);
      if (options.type !== 'error') return showMessageBox(...args);
      process.stderr.write(`Desktop startup error: ${options.message} ${options.detail}\n`);
      return { response: options.buttons.indexOf('Quit'), checkboxChecked: false };
    };
  });
  const page = await electron.firstWindow();
  const rendererErrors = [];
  let browserLog = '';
  const record = (message) => { browserLog = `${browserLog}${message}\n`.slice(-16_384); };
  page.on('pageerror', (error) => rendererErrors.push(error.message));
  page.on('console', (message) => {
    if (['error', 'warning'].includes(message.type())) record(message.text());
  });
  page.on('requestfailed', (request) => record(`${request.url()}: ${request.failure()?.errorText}`));
  page.on('response', (response) => {
    if (['document', 'script', 'stylesheet'].includes(response.request().resourceType())) {
      record(`${response.status()} ${response.headers()['content-type']} ${response.url()}`);
    }
  });
  try {
    await page.waitForURL('frisket://app/**', { timeout: 300_000 });
    await expect(page.getByTestId('home-screen')).toBeVisible();
    const disclosure = page.getByTestId('product-telemetry-disclosure');
    if (await disclosure.isVisible()) {
      await disclosure.getByRole('checkbox').uncheck();
      await disclosure.getByRole('button', { name: 'Continue' }).click();
    }
    return { electron, page, rendererErrors };
  } catch (error) {
    await testInfo.attach('startup-stderr', { body: stderr, contentType: 'text/plain' });
    await testInfo.attach('startup-browser', {
      body: `${rendererErrors.join('\n')}\n${browserLog}`, contentType: 'text/plain',
    });
    await page.screenshot({ path: testInfo.outputPath('startup-failure.png') }).catch(() => {});
    await electron.close().catch(() => {});
    throw error;
  }
}

async function descendants(pid) {
  const { stdout } = await exec('/bin/ps', ['-axo', 'pid=,ppid=']);
  const rows = stdout.trim().split('\n').map((line) => line.trim().split(/\s+/).map(Number));
  const owned = new Set([pid]);
  for (let previous = -1; previous !== owned.size;) {
    previous = owned.size;
    for (const [child, parent] of rows) if (owned.has(parent)) owned.add(child);
  }
  return [...owned];
}

async function quit(electron, closeWindow = false) {
  // Playwright disposes the Electron channel when the process exits.
  const child = electron.process();
  const pids = await descendants(child.pid);
  if (closeWindow) {
    // Exercise the native close button, which app.quit() bypasses.
    await electron.evaluate(({ BrowserWindow }) => {
      setImmediate(() => BrowserWindow.getAllWindows()[0]?.close());
    });
    await expect.poll(() => child.exitCode, { timeout: 20_000 }).toBe(0);
  } else {
    await electron.close();
  }
  await expect.poll(() => pids.filter((pid) => {
    try { process.kill(pid, 0); return true; } catch { return false; }
  }), { timeout: 20_000 }).toEqual([]);
}

async function openRegex(page) {
  const ribbon = page.getByTestId('act-ribbon');
  const tile = page.getByTestId('ribbon-action-map.regex_extract');
  await expect(ribbon).toBeVisible();
  const tabs = ribbon.getByRole('tab');
  await expect(async () => {
    for (let index = 0; index < await tabs.count() && !await tile.isVisible(); index++) {
      await tabs.nth(index).click();
    }
    expect(await tile.isVisible()).toBeTruthy();
  }).toPass({ timeout: 20_000, intervals: [100, 250, 500] });
  await tile.click();
  await page.getByTestId('field-input_columns').click();
  await page.getByTestId('field-input_columns-menu').getByRole('option').filter({ hasText: 'note' }).click();
  await page.keyboard.press('Escape');
  await page.getByTestId('field-pattern').fill('\\$[0-9,]+');
}

async function sheet(page, projectId, sheetId) {
  return page.evaluate(async ({ projectId, sheetId }) => {
    const response = await fetch(`/api/projects/${projectId}/sheets/${sheetId}/data?offset=0&limit=20`);
    if (!response.ok) throw new Error(`Sheet read failed: ${response.status}`);
    return response.json();
  }, { projectId, sheetId });
}

function extracted(data) {
  const column = data.columns.find((column) => column.name === 'extracted');
  return column ? data.rows[0].cells[String(column.id)] : null;
}

test('installed app imports, runs its worker, exports, quits and reopens', async ({}, testInfo) => {
  expect(process.platform).toBe('darwin');
  expect(process.arch).toBe('arm64');
  expect(typeof appPath).toBe('string');
  expect(typeof profile).toBe('string');
  expect(path.isAbsolute(appPath)).toBeTruthy();
  expect(path.isAbsolute(profile)).toBeTruthy();
  const first = await launch(testInfo);
  let running = first.electron;
  try {
    const page = first.page;
    expect(await running.evaluate(({ app }) => app.getName())).toBe('Frisket Desktop');
    expect(await running.evaluate(({ app }) => app.getPath('userData'))).toBe(profile);
    expect(await running.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].getTitle())).toBe('Frisket Desktop');
    expect(await page.evaluate(() => [typeof window.require, typeof window.process])).toEqual(['undefined', 'undefined']);
    await page.evaluate(() => navigator.clipboard.writeText('Desktop clipboard smoke'));
    expect(await running.evaluate(({ clipboard }) => clipboard.readText())).toBe('Desktop clipboard smoke');
    const popupPromise = running.waitForEvent('window');
    await page.evaluate(() => window.open('/api/health', '_blank'));
    const popup = await popupPromise;
    await popup.waitForURL('frisket://app/api/health');
    await popup.close();
    const pids = await descendants(running.process().pid);
    const { stdout: listeners } = await exec('/usr/sbin/lsof', ['-nP', '-a', '-p', pids.join(','), '-iTCP', '-sTCP:LISTEN', '-Fn']);
    const ports = [...listeners.matchAll(/^n127\.0\.0\.1:(\d+)$/gm)].map((match) => Number(match[1]));
    const statuses = await Promise.all(ports.map(async (port) => {
      const response = await fetch(`http://127.0.0.1:${port}/api/health`);
      return response.status;
    }));
    expect(statuses).toContain(401);
    await page.getByTestId('home-new-project').click();
    await page.getByTestId('new-project-name').fill('Desktop beta smoke');
    await page.getByTestId('create-project').click();
    await page.waitForURL(/\/p\//);
    const projectId = new URL(page.url()).pathname.split('/')[2];
    if (!await page.getByTestId('import-csv-input').count()) {
      await page.getByTestId('ribbon-tab-data').click();
      await page.getByTestId('ribbon-command-import').click();
    }
    const uploaded = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname.endsWith('/import/csv'));
    await page.getByTestId('import-csv-input').setInputFiles({
      name: 'desktop-smoke.csv', mimeType: 'text/csv',
      buffer: Buffer.from('note\n"contract worth $4,200 total"\n"routine agenda item"\n'),
    });
    await expect(page.getByTestId('import-csv-preview')).toBeVisible();
    await page.getByTestId('import-csv-confirm').click();
    const response = await uploaded;
    expect(response.ok()).toBeTruthy();
    const imported = await response.json();
    const sheetId = imported.sheet_id;
    expect(sheetId).toBeTruthy();
    await expect(page.getByTestId('sheet-stats')).toHaveText(/2 rows · 1 columns?/);
    await openRegex(page);
    await page.getByTestId('generated-action-run').click();
    await expect.poll(async () => extracted(await sheet(page, projectId, sheetId)), { timeout: 60_000 }).toBe('$4,200');
    await expect(page.getByTestId('sheet-stats')).toHaveText('2 rows · 2 columns');
    await page.screenshot({ path: testInfo.outputPath('completed-action.png') });

    const exportPath = testInfo.outputPath('desktop-smoke.csv');
    await mkdir(path.dirname(exportPath), { recursive: true });
    // Supply a destination at the documented download boundary; keep the app handler.
    await running.evaluate(({ session }, destination) => {
      session.defaultSession.once('will-download', (_event, item) => item.setSavePath(destination));
    }, exportPath);
    await page.getByTestId('switch-project').click();
    await page.getByTestId('export-menu-open').click();
    await page.getByTestId('export-target-csv').click();
    await page.getByTestId('export-dataset-download').click();
    await expect.poll(async () => readFile(exportPath, 'utf8').catch(() => ''), { timeout: 20_000 }).toContain('$4,200');
    expect(first.rendererErrors).toEqual([]);
    await quit(running);
    running = undefined;

    const second = await launch(testInfo);
    running = second.electron;
    await second.page.getByTestId(`project-${projectId}`).click();
    await expect(second.page.getByTestId('grid')).toBeVisible();
    expect(extracted(await sheet(second.page, projectId, sheetId))).toBe('$4,200');
    await second.page.screenshot({ path: testInfo.outputPath('reopened-project.png') });
    expect(second.rendererErrors).toEqual([]);
    await quit(running, true);
    running = undefined;
  } finally {
    if (running) await running.close().catch(() => {});
  }
});
