import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import { createProject, importCsv, listSheets, openImportWorkspace, sheetData, uniqueName } from './helpers';

test('CSV upload previews encoding before the explicit import', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('csv-upload-preview'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);

  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openImportWorkspace(page);
  await expect(page.getByTestId('import-workspace')).toBeVisible();
  await expect(page.getByTestId('import-file-picker')).toContainText('Choose one file');

  const previewResponse = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname.endsWith(`/api/projects/${pid}/import/csv/preview`) &&
      !new URL(response.url()).search,
  );
  const csvBytes = Buffer.concat([
    Buffer.from('city,quote,score\nAlbany,"'),
    Buffer.from([0x93]),
    Buffer.from('hello'),
    Buffer.from([0x94]),
    Buffer.from('",4\nTroy,plain,7\n'),
  ]);
  await page.getByTestId('import-csv-input').setInputFiles({
    name: 'uploaded.csv',
    mimeType: 'text/csv',
    buffer: csvBytes,
  });

  const preview = await previewResponse;
  expect(preview.ok()).toBeTruthy();
  await expect(page.getByTestId('import-csv-preview')).toBeVisible();
  await expect(page.getByTestId('import-csv-detected')).toContainText('Detected cp1252');
  await expect(page.getByTestId('import-csv-preview-table').getByRole('cell').nth(1))
    .toHaveText('“hello”');

  const macPreview = page.waitForResponse((response) =>
    response.url().includes('/import/csv/preview?encoding=mac_roman'));
  await page.getByTestId('import-csv-encoding').selectOption('mac_roman');
  expect((await macPreview).ok()).toBeTruthy();
  await expect(page.getByTestId('import-csv-preview-table').getByRole('cell').nth(1))
    .not.toHaveText('“hello”');

  const windowsPreview = page.waitForResponse((response) =>
    response.url().includes('/import/csv/preview?encoding=cp1252'));
  await page.getByTestId('import-csv-encoding').selectOption('cp1252');
  expect((await windowsPreview).ok()).toBeTruthy();
  await expect(page.getByTestId('import-csv-preview-table').getByRole('cell').nth(1))
    .toHaveText('“hello”');

  const uploadResponse = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname.endsWith(`/api/projects/${pid}/import/csv`),
  );
  await page.getByTestId('import-csv-confirm').click();
  const response = await uploadResponse;
  expect(response.ok()).toBeTruthy();
  expect(new URL(response.url()).searchParams.get('encoding')).toBe('cp1252');
  await expect(page.getByTestId('sheet-stats')).toHaveText('2 rows · 3 columns', {
    timeout: 15_000,
  });

  const body = await response.json();
  expect(body.rows).toBe(2);
  expect(body.columns).toEqual(['city', 'quote', 'score']);

  const uploaded = (await listSheets(page.request, pid)).find((sheet) => sheet.name === 'uploaded');
  expect(uploaded).toBeTruthy();
  const data = await sheetData(page.request, pid, uploaded!.id);
  const city = data.columns.find((column) => column.name === 'city');
  const score = data.columns.find((column) => column.name === 'score');
  const quote = data.columns.find((column) => column.name === 'quote');
  expect(city).toBeTruthy();
  expect(score).toBeTruthy();
  expect(quote).toBeTruthy();
  expect(data.rows[0].cells[String(city!.id)]).toBe('Albany');
  expect(data.rows[0].cells[String(quote!.id)]).toBe('“hello”');
  expect(data.rows[1].cells[String(score!.id)]).toBe(7);
});

test('CSV update previews blank clearing, then preserves blanks on explicit apply', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('csv-update-preview'));
  const sheetId = await importCsv(page.request, pid, 'people.csv', 'id,name,note\na,Ada,original\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openImportWorkspace(page);
  await page.getByTestId('import-csv-input').setInputFiles({
    name: 'corrections.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('id,name,note\na,,replacement\nmissing,Nobody,ignored\n'),
  });

  await expect(page.getByTestId('import-csv-preview')).toBeVisible();
  await page.locator('#import-csv-destination').selectOption(String(sheetId));
  await expect(page.locator('#import-csv-destination').locator('option:checked')).toHaveText('people');
  await page.locator('#import-file-destination-mode').selectOption('update');
  await expect(page.getByText('Preview exact matches before updating existing rows; unmatched rows will be left unchanged.')).toBeVisible();
  await page.getByLabel('id update action').selectOption('match');
  await page.getByLabel('note update action').selectOption('update');
  const noteMapping = page.getByText('note', { exact: true }).locator('..').locator('select').first();
  await noteMapping.selectOption('');

  let releasePreview!: () => void;
  const previewGate = new Promise<void>((resolve) => { releasePreview = resolve; });
  await page.route(`**/api/projects/${pid}/import/csv/update/preview`, async (route) => {
    await previewGate;
    await route.continue();
  }, { times: 1 });
  const clearPreviewResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/api/projects/${pid}/import/csv/update/preview`));
  await page.getByTestId('import-csv-confirm').click();
  await expect(page.locator('#import-csv-destination')).toBeDisabled();
  await expect(page.locator('#import-file-destination-mode')).toBeDisabled();
  await expect(page.getByLabel('id update action')).toBeDisabled();
  await expect(page.getByLabel('Keep existing values where imported cells are blank')).toBeDisabled();
  releasePreview();
  expect((await clearPreviewResponse).ok()).toBeTruthy();
  await expect(page.getByTestId('import-file-update-preview')).toContainText('1 cells cleared');
  await expect(page.getByTestId('import-file-update-preview')).toContainText('1 matched · 1 unmatched');

  await page.getByLabel('Keep existing values where imported cells are blank').check();
  const preservePreviewResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/api/projects/${pid}/import/csv/update/preview`));
  await page.getByTestId('import-csv-confirm').click();
  expect((await preservePreviewResponse).ok()).toBeTruthy();
  await expect(page.getByTestId('import-file-update-preview')).toContainText('0 cells cleared');

  let releaseApply!: () => void;
  const applyGate = new Promise<void>((resolve) => { releaseApply = resolve; });
  await page.route(`**/api/projects/${pid}/import/csv/update`, async (route) => {
    await applyGate;
    await route.continue();
  }, { times: 1 });
  const applyResponse = page.waitForResponse((response) =>
    response.url().endsWith(`/api/projects/${pid}/import/csv/update`));
  await page.getByTestId('import-csv-confirm').click();
  await expect(page.getByTestId('import-file-picker')).toBeDisabled();
  await expect(page.getByTestId('import-workspace-close')).toBeDisabled();
  await expect(page.locator('#import-file-destination-mode')).toBeDisabled();
  await page.getByTestId('import-mode-files').click();
  await expect(page.getByTestId('import-mode-csv')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('import-file-update-preview')).toBeVisible();
  releaseApply();
  expect((await applyResponse).ok()).toBeTruthy();

  const data = await sheetData(page.request, pid, sheetId);
  const name = data.columns.find((column) => column.name === 'name')!;
  const note = data.columns.find((column) => column.name === 'note')!;
  expect(data.rows).toHaveLength(1);
  expect(data.rows[0].cells[String(name.id)]).toBe('Ada');
  expect(data.rows[0].cells[String(note.id)]).toBe('original');
});

test('choosing a new CSV cancels an in-flight update preview', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('csv-update-cancel'));
  const sheetId = await importCsv(page.request, pid, 'people.csv', 'id,name\na,Ada\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openImportWorkspace(page);
  await page.getByTestId('import-csv-input').setInputFiles({
    name: 'first.csv', mimeType: 'text/csv', buffer: Buffer.from('id,name\na,Augusta\n'),
  });
  await expect(page.getByTestId('import-csv-preview')).toBeVisible();
  await page.locator('#import-csv-destination').selectOption(String(sheetId));
  await page.locator('#import-file-destination-mode').selectOption('update');
  await page.getByLabel('id update action').selectOption('match');

  let releasePreview!: () => void;
  const previewGate = new Promise<void>((resolve) => { releasePreview = resolve; });
  await page.route(`**/api/projects/${pid}/import/csv/update/preview`, async (route) => {
    await previewGate;
    await route.continue();
  }, { times: 1 });
  await page.getByTestId('import-csv-confirm').click();
  await expect(page.locator('#import-file-destination-mode')).toBeDisabled();

  const nextFilePreview = page.waitForResponse((response) =>
    response.url().endsWith(`/api/projects/${pid}/import/csv/preview`));
  await page.getByTestId('import-csv-input').setInputFiles({
    name: 'second.csv', mimeType: 'text/csv', buffer: Buffer.from('id,name\na,Grace\n'),
  });
  releasePreview();
  expect((await nextFilePreview).ok()).toBeTruthy();
  await expect(page.getByTestId('import-csv-preview')).toContainText('second.csv');
  await expect(page.getByTestId('import-file-update-preview')).toHaveCount(0);
});

test('one-file import detects Excel after selection', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('xlsx-upload-detection'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openImportWorkspace(page);
  const previewResponse = page.waitForResponse((response) =>
    response.request().method() === 'POST' && response.url().endsWith(`/api/projects/${pid}/import/xlsx/preview`));
  await page.getByTestId('import-csv-input').setInputFiles({
    name: 'people.xlsx',
    // File portals may report a generic or wrong MIME type for a known suffix.
    mimeType: 'text/csv',
    buffer: readFileSync(new URL('../fixtures/import-people.xlsx', import.meta.url)),
  });
  expect((await previewResponse).ok()).toBeTruthy();
  await expect(page.getByTestId('import-csv-preview-table')).toContainText('Ada');
  const imported = page.waitForResponse((response) =>
    response.request().method() === 'POST' && response.url().endsWith(`/api/projects/${pid}/import/xlsx`));
  await page.getByTestId('import-csv-confirm').click();
  const response = await imported;
  expect(response.ok()).toBeTruthy();
  const result = await response.json();
  expect(result).toMatchObject({ rows: 1, columns: ['name', 'score'] });
  const data = await sheetData(page.request, pid, result.sheet_id);
  const score = data.columns.find((column) => column.name === 'score');
  expect(data.rows[0].cells[String(score!.id)]).toBe(7);
  await expect(page.getByTestId('import-workspace-dialog')).toHaveCount(0);
});
