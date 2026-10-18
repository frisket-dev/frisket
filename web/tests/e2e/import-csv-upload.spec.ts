import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import {
  commitTableImport, createProject, importCsv, listSheets, openImportWorkspace,
  previewTableFile, reviewTableColumns, sheetData, uniqueName,
} from './helpers';

test('table upload previews encoding and imports only after all review steps', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('table-upload-preview'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible();
  await openImportWorkspace(page);
  const csvBytes = Buffer.concat([
    Buffer.from('city,quote,score\nAlbany,"'), Buffer.from([0x93]),
    Buffer.from('hello'), Buffer.from([0x94]), Buffer.from('",4\nTroy,plain,7\n'),
  ]);
  await previewTableFile(page, { name: 'uploaded.csv', mimeType: 'text/csv', buffer: csvBytes });
  const rows = page.getByRole('region', { name: 'Check rows', exact: true });
  const encoding = rows.getByLabel('Character encoding');
  await expect(encoding.locator('option:checked')).toContainText(/cp1252|Windows-1252/);
  await expect(rows.getByRole('table')).toContainText('“hello”');
  await encoding.selectOption('mac-roman');
  await expect(rows.getByRole('table')).not.toContainText('“hello”');
  await encoding.selectOption('windows-1252');
  await expect(rows.getByRole('table')).toContainText('“hello”');
  expect(await listSheets(page.request, pid)).toHaveLength(1);
  await reviewTableColumns(page);
  expect(await listSheets(page.request, pid)).toHaveLength(1);
  const sheetId = await commitTableImport(page, pid);
  await expect.poll(async () => (await sheetData(page.request, pid, sheetId)).total).toBe(2);
  const data = await sheetData(page.request, pid, sheetId);
  const cell = (row: number, name: string) => data.rows[row].cells[String(data.columns.find(column => column.name === name)!.id)];
  expect(cell(0, 'city')).toBe('Albany');
  expect(cell(0, 'quote')).toBe('“hello”');
  expect(cell(1, 'score')).toBe(7);
});

test('existing-sheet import matches columns and skips duplicate keys without updating existing cells', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('table-append-dedup'));
  const sheetId = await importCsv(page.request, pid, 'people.csv', 'id,name,note\na,Ada,original\n');
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible();
  await openImportWorkspace(page);
  await previewTableFile(page, {
    name: 'more.csv', mimeType: 'text/csv',
    buffer: Buffer.from('id,name,note\na,Augusta,replacement\nb,Grace,new\n'),
  });
  await reviewTableColumns(page);
  const destination = page.locator('.table-import-flow').getByRole('region', { name: 'Import', exact: true });
  await destination.getByRole('radio', { name: 'Add to an existing sheet' }).check();
  await destination.getByLabel('Sheet', { exact: true }).selectOption(String(sheetId));
  await expect(destination.getByLabel('Map id')).toHaveValue(/^existing:/);
  await destination.getByRole('checkbox', { name: /Skip rows already in people/ }).check();
  await destination.getByLabel('Duplicate key', { exact: true }).selectOption('0');
  await expect(destination.getByText('1 row found', { exact: true })).toBeVisible();
  expect(await commitTableImport(page, pid)).toBe(sheetId);
  await expect.poll(async () => (await sheetData(page.request, pid, sheetId)).total).toBe(2);
  const data = await sheetData(page.request, pid, sheetId);
  const names = data.columns.map(column => column.name);
  expect(names).toEqual(['id', 'name', 'note']);
  const value = (row: number, column: string) => data.rows[row].cells[String(data.columns.find(item => item.name === column)!.id)];
  expect(value(0, 'name')).toBe('Ada');
  expect(value(0, 'note')).toBe('original');
  expect(value(1, 'name')).toBe('Grace');
});

test('pending reader settings block navigation until the updated preview arrives', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('table-settings-race'));
  await page.goto(`/p/${pid}`);
  await page.getByTestId('import-workspace-open').click();
  await previewTableFile(page, { name: 'rows.csv', mimeType: 'text/csv', buffer: Buffer.from('id,name\na,Ada\n') });
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const requested = page.waitForRequest(request => request.method() === 'PATCH' && request.url().endsWith('/settings'));
  await page.route(`**/api/projects/${pid}/import/table-imports/*/settings`, async route => {
    await gate;
    await route.continue();
  }, { times: 1 });
  try {
    await page.getByLabel('Character encoding').selectOption('windows-1252');
    await requested;
    await expect(page.getByRole('button', { name: 'Columns →', exact: true })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeDisabled();
  } finally {
    release();
  }
  await expect(page.getByRole('button', { name: 'Columns →', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Back', exact: true }).click();
  await previewTableFile(page, { name: 'second.csv', mimeType: 'text/csv', buffer: Buffer.from('id,name\nb,Grace\n') });
  await expect(page.getByRole('region', { name: 'Check rows', exact: true }).getByRole('table')).toContainText('Grace');
  await expect(page.getByRole('region', { name: 'Check rows', exact: true }).getByRole('table')).not.toContainText('Ada');
  await reviewTableColumns(page);
  const sheetId = await commitTableImport(page, pid);
  await expect.poll(async () => (await sheetData(page.request, pid, sheetId)).total).toBe(1);
  expect((await listSheets(page.request, pid)).map(sheet => sheet.name)).toEqual(['second']);
});

test('one-file import detects Excel from its suffix and preserves numeric values', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('xlsx-upload-detection'));
  await page.goto(`/p/${pid}`);
  await page.getByTestId('import-workspace-open').click();
  await previewTableFile(page, {
    name: 'people.xlsx', mimeType: 'text/csv',
    buffer: readFileSync(new URL('../fixtures/import-people.xlsx', import.meta.url)),
  });
  await expect(page.getByRole('region', { name: 'Check rows', exact: true }).getByRole('table')).toContainText('Ada');
  await expect(page.getByLabel('Character encoding')).toHaveCount(0);
  await reviewTableColumns(page);
  const sheetId = await commitTableImport(page, pid);
  await expect.poll(async () => (await sheetData(page.request, pid, sheetId)).total).toBe(1);
  const data = await sheetData(page.request, pid, sheetId);
  expect(data.columns.map(column => column.name)).toEqual(['name', 'score']);
  expect(data.rows[0].cells[String(data.columns.find(column => column.name === 'score')!.id)]).toBe(7);
});
