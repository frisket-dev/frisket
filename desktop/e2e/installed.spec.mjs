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
    executablePath: path.join(appPath, 'Contents/MacOS/Frisket'),
    args: [`--user-data-dir=${profile}`],
    chromiumSandbox: true,
    timeout: 60_000,
    env: { ...process.env, UV_OFFLINE: '1' },
  });
  let stderr = '';
  electron.process().stderr.on('data', (chunk) => {
    stderr = `${stderr}${chunk}`.slice(-16_384);
  });
  const page = await electron.firstWindow();
  page.on('pageerror', (error) => console.error(`Renderer: ${error.message}`));
  try {
    await page.waitForURL('frisket://app/**', { timeout: 300_000 });
    await expect(page.getByTestId('home-screen')).toBeVisible();
    const disclosure = page.getByTestId('product-telemetry-disclosure');
    if (await disclosure.isVisible()) {
      await disclosure.getByRole('checkbox').uncheck();
      await disclosure.getByRole('button', { name: 'Continue' }).click();
    }
    return { electron, page };
  } catch (error) {
    await testInfo.attach('startup-stderr', { body: stderr, contentType: 'text/plain' });
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

async function quit(electron) {
  const pids = await descendants(electron.process().pid);
  const closed = electron.waitForEvent('close');
  await electron.evaluate(({ app }) => app.quit()).catch(() => {});
  await closed;
  await expect.poll(() => pids.filter((pid) => {
    try { process.kill(pid, 0); return true; } catch { return false; }
  }), { timeout: 20_000 }).toEqual([]);
}

async function openRegex(page) {
  const ribbon = page.getByTestId('act-ribbon');
  const tile = page.getByTestId('ribbon-action-map.regex_extract');
  await expect(ribbon).toBeVisible();
  const tabs = ribbon.getByRole('tab');
  for (let index = 0; index < await tabs.count() && !await tile.isVisible(); index++) {
    await tabs.nth(index).click();
  }
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
  expect(path.isAbsolute(appPath)).toBeTruthy();
  expect(path.isAbsolute(profile)).toBeTruthy();
  const first = await launch(testInfo);
  let running = first.electron;
  try {
    const page = first.page;
    expect(await page.evaluate(() => [typeof window.require, typeof window.process])).toEqual(['undefined', 'undefined']);
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
    // Automate only the native save chooser; the ordinary download handler runs.
    await running.evaluate(({ dialog }, destination) => {
      dialog.showSaveDialog = async () => ({ canceled: false, filePath: destination });
    }, exportPath);
    await page.getByTestId('switch-project').click();
    await page.getByTestId('export-menu-open').click();
    await page.getByTestId('export-target-csv').click();
    await page.getByTestId('export-dataset-download').click();
    await expect.poll(async () => readFile(exportPath, 'utf8').catch(() => ''), { timeout: 20_000 }).toContain('$4,200');
    await quit(running);
    running = undefined;

    const second = await launch(testInfo);
    running = second.electron;
    await second.page.getByTestId(`project-${projectId}`).click();
    await expect(second.page.getByTestId('grid')).toBeVisible();
    expect(extracted(await sheet(second.page, projectId, sheetId))).toBe('$4,200');
    await second.page.screenshot({ path: testInfo.outputPath('reopened-project.png') });
    await quit(running);
    running = undefined;
  } finally {
    if (running) await running.close().catch(() => {});
  }
});
