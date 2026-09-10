import fs from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openFriendlyFilterSidebar,
  openAdvancedSortPanel,
  openProject,
  sheetColumns,
  uniqueName,
} from './helpers';

test('project menu exports the current sheet as CSV and the work log as Markdown', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('dataset-export'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'export rows.csv',
    'name,note\nAda,"hello, world"\nGrace,plain\n',
  );
  await openProject(page, pid, sheetId);

  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();

  const csvDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-menu-open').click();
  await page.getByTestId('export-target-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await page.getByTestId('export-dataset-download').click();
  const csvDownload = await csvDownloadPromise;
  expect(csvDownload.suggestedFilename()).toBe('export-rows.csv');
  const csvPath = await csvDownload.path();
  expect(csvPath).toBeTruthy();
  const csvBytes = await fs.readFile(csvPath!);
  expect([...csvBytes.subarray(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
  expect(csvBytes.toString('utf8')).toContain('Ada,"hello, world"');

  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();

  const logDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-work-log').click();
  const logDownload = await logDownloadPromise;
  expect(logDownload.suggestedFilename()).toMatch(/-work-log\.md$/);
  const logPath = await logDownload.path();
  expect(logPath).toBeTruthy();
  const logText = await fs.readFile(logPath!, 'utf8');
  expect(logText).toContain('# Work log:');
  expect(logText).toContain('## Operation History');
  expect(logText).toContain('| 1 | import.rows | import.csv | applied |');
  expect(logText).toContain('## Action Receipts');
  expect(logText).toContain('| import.csv | completed | 1 |');
  expect(logText).not.toContain('Recipe');
});

test('project menu exports either the entire sheet or the active grid view as CSV', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('dataset-view-export'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'view rows.csv',
    [
      'title,status,published',
      'A start,todo,2026-01-01',
      'B done,done,2026-02-01',
      'C done,done,2026-03-01',
    ].join('\n') + '\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const columns = await sheetColumns(page.request, pid, sheetId);
  await openFriendlyFilterSidebar(page, columns, 'status');
  const filterResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      url.searchParams.has('filter')
    );
  });
  await page.getByTestId('facet-check-status-done').check();
  await filterResponse;

  await openAdvancedSortPanel(page, columns, 'title');
  await page.getByTestId('grid-sort-column').selectOption('published');
  await page.getByTestId('grid-sort-direction').selectOption('desc');
  const sortResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      url.searchParams.has('filter') &&
      url.searchParams.has('sort')
    );
  });
  await page.getByTestId('apply-grid-sort').click();
  await sortResponse;

  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();
  const fullDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-menu-open').click();
  await page.getByTestId('export-target-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await page.getByTestId('export-dataset-download').click();
  const fullDownload = await fullDownloadPromise;
  const fullPath = await fullDownload.path();
  expect(fullPath).toBeTruthy();
  const fullText = await fs.readFile(fullPath!, 'utf8');
  expect(fullText).toContain('A start,todo,2026-01-01');
  expect(fullText.indexOf('B done,done,2026-02-01')).toBeLessThan(
    fullText.indexOf('C done,done,2026-03-01'),
  );

  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();
  const viewDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-menu-open').click();
  await page.getByTestId('export-target-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await page.getByTestId('export-scope-current-view').check();
  await page.getByTestId('export-dataset-download').click();
  const viewDownload = await viewDownloadPromise;
  const viewPath = await viewDownload.path();
  expect(viewPath).toBeTruthy();
  const viewText = await fs.readFile(viewPath!, 'utf8');
  expect(viewText).not.toContain('A start,todo,2026-01-01');
  expect(viewText.indexOf('C done,done,2026-03-01')).toBeLessThan(
    viewText.indexOf('B done,done,2026-02-01'),
  );
});

test('export picker downloads selected sheets as Excel tabs or a CSV ZIP', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('dataset-multi-export'));
  const casesId = await importCsv(
    page.request,
    pid,
    'Cases 2026.csv',
    'person,status\nJosé,open\n李雷,closed\n',
  );
  const meetingsId = await importCsv(
    page.request,
    pid,
    'Meeting notes.csv',
    'speaker,quote\nZoë,“smart quotes”\n',
  );
  await openProject(page, pid, casesId);

  const openExport = async () => {
    await page.getByTestId('switch-project').click();
    await page.getByTestId('export-menu-open').click();
    await page.getByTestId('export-target-csv').click();
    await expect(page.getByTestId('export-data-modal')).toBeVisible();
  };

  await openExport();
  await expect(page.getByTestId(`export-sheet-${casesId}`)).toBeChecked();
  await expect(page.getByTestId(`export-sheet-${meetingsId}`)).not.toBeChecked();
  await page.getByTestId('export-format-xlsx').check();
  await page.getByTestId('export-select-all-sheets').click();
  await expect(page.getByTestId('export-scope-current-view')).toBeDisabled();
  await expect(page.getByTestId('export-dataset-download')).toContainText(
    'Export 2 sheets to Excel',
  );
  const excelDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-dataset-download').click();
  const excelDownload = await excelDownloadPromise;
  expect(excelDownload.suggestedFilename()).toMatch(/\.xlsx$/);
  const excelPath = await excelDownload.path();
  expect(excelPath).toBeTruthy();
  expect((await fs.readFile(excelPath!)).subarray(0, 2).toString()).toBe('PK');

  await openExport();
  await page.getByTestId('export-select-all-sheets').click();
  await expect(page.getByTestId('export-dataset-download')).toContainText(
    'Export 2 CSVs as ZIP',
  );
  const zipDownloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-dataset-download').click();
  const zipDownload = await zipDownloadPromise;
  expect(zipDownload.suggestedFilename()).toMatch(/-csv\.zip$/);
  const zipPath = await zipDownload.path();
  expect(zipPath).toBeTruthy();
  expect((await fs.readFile(zipPath!)).subarray(0, 2).toString()).toBe('PK');
});
