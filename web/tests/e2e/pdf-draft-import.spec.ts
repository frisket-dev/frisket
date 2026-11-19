import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  listSheets,
  openImportWorkspace,
  openProject,
  sheetData,
  textPdf,
  uniqueName,
} from './helpers';

const PDFS = [
  {
    name: 'council-minutes.pdf',
    lines: ['Riverton City Council minutes', 'The council approved the transit budget.'],
  },
  {
    name: 'planning-memo.pdf',
    lines: ['Planning department memo', 'The zoning hearing moved to March.'],
  },
] as const;

test('two text PDFs import as one document each into a new sheet', async ({ page }) => {
  test.setTimeout(180_000);
  const pid = await createProject(page.request, uniqueName('e2e-pdf-draft-import'));
  const seedSheetId = await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  const sheetName = uniqueName('Council PDFs');

  await openProject(page, pid, seedSheetId);
  await openImportWorkspace(page);
  const tableSource = page.getByRole('region', { name: 'Add data', exact: true });
  const filesMode = page.getByTestId('import-mode-files');
  await expect(tableSource.or(filesMode).first()).toBeVisible();
  if (await tableSource.isVisible()) {
    await page.getByRole('button', { name: 'Multiple files', exact: true }).click();
  }
  await expect(filesMode).toHaveAttribute('aria-selected', 'true');

  await page.getByTestId('import-workspace').getByTestId('import-file-input').setInputFiles(
    PDFS.map((pdf) => ({
      name: pdf.name,
      mimeType: 'application/pdf',
      buffer: textPdf([[...pdf.lines]]),
    })),
  );

  const flow = page.getByTestId('pdf-batch-flow');
  await expect(flow).toHaveAttribute('data-stage', 'add', { timeout: 30_000 });
  await expect(flow.getByText('Copying complete', { exact: true })).toBeVisible({ timeout: 60_000 });
  await expect(flow.getByText(/2 files checked · 2 files copied/)).toBeVisible({ timeout: 60_000 });
  const checkText = flow.getByRole('button', { name: 'Check text →', exact: true });
  await expect(checkText).toBeEnabled({ timeout: 60_000 });
  await checkText.click();
  await expect(flow).toHaveAttribute('data-stage', 'check', { timeout: 30_000 });

  const noOcr = flow.getByRole('radio', { name: /^None/ });
  await noOcr.click();
  await expect(noOcr).toBeChecked();
  await expect(flow.getByRole('radio', { name: /^Keep documents/ })).toBeChecked();
  await expect(flow.getByText('2 files → 2 rows', { exact: true })).toBeVisible({ timeout: 60_000 });
  await flow.getByRole('button', { name: 'Continue →', exact: true }).click();

  await expect(flow).toHaveAttribute('data-stage', 'destination');
  await expect(flow.getByRole('radio', { name: 'New sheet' })).toBeChecked();
  await flow.getByRole('textbox', { name: 'Sheet name' }).fill(sheetName);
  await flow.getByRole('button', { name: 'Search setup →', exact: true }).click();

  await expect(flow).toHaveAttribute('data-stage', 'search', { timeout: 30_000 });
  const semantic = flow.getByRole('checkbox', { name: /Search by meaning/ });
  if (await semantic.isChecked()) await semantic.uncheck();
  await expect(semantic).not.toBeChecked();
  const importButton = flow.getByRole('button', { name: 'Import 2 files', exact: true });
  await expect(importButton).toBeEnabled({ timeout: 60_000 });
  await importButton.click();

  await expect(flow).toHaveCount(0, { timeout: 90_000 });
  await expect(page.getByTestId('import-workspace-dialog')).toHaveCount(0);

  let importedSheetId: number | undefined;
  await expect.poll(async () => {
    const sheet = (await listSheets(page.request, pid)).find((item) => item.name === sheetName);
    importedSheetId = sheet?.id;
    return sheet ? (await sheetData(page.request, pid, sheet.id)).total : null;
  }, { timeout: 90_000 }).toBe(2);
  expect(importedSheetId).not.toBe(seedSheetId);
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${importedSheetId}$`));

  const data = await sheetData(page.request, pid, importedSheetId!);
  expect(data.columns.map((column) => column.name)).toEqual(
    expect.arrayContaining(['filename', 'media', 'pages', 'source', 'text']),
  );
  const filenameColumn = String(data.columns.find((column) => column.name === 'filename')!.id);
  const textColumn = String(data.columns.find((column) => column.name === 'text')!.id);
  const textByFilename = new Map(data.rows.map((row) => [
    row.cells[filenameColumn],
    String(row.cells[textColumn] ?? ''),
  ]));
  expect([...textByFilename.keys()].toSorted()).toEqual(PDFS.map((pdf) => pdf.name).toSorted());
  for (const pdf of PDFS) {
    for (const line of pdf.lines) expect(textByFilename.get(pdf.name)).toContain(line);
  }

  await openProject(page, pid, importedSheetId);
  await expect(page.getByTestId('sheet-stats')).toContainText('2 rows');
});
