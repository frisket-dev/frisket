import path from 'node:path';
import { expect, type Page } from '@playwright/test';
import {
  commitTableImport,
  createProject,
  importCsv,
  openImportWorkspace,
  openProject,
  previewTableFile,
  sheetData,
  textPdf,
} from '../e2e/helpers';

type Shot = (page: Page, family: string, name: string, source: string) => Promise<void>;
const tableSource = 'src/components/importWorkspace/tableImport/TableImportSteps.tsx';
const batchSource = 'src/components/importWorkspace/pdfBatch/PdfBatchSteps.tsx';
const packetSource = 'src/components/importWorkspace/pdfPacket/';

async function freshImport(page: Page, label: string) {
  const pid = await createProject(page.request, `Inventory ${label}`);
  const sheet = await importCsv(page.request, pid, 'existing.csv', 'name,amount\nAda,1200\nMax,50\n');
  await openProject(page, pid, sheet);
  await openImportWorkspace(page);
  return pid;
}

/** Real local API workflows. These captures do not run OCR, embedding or fetch feeds. */
export async function captureImportScenes(page: Page, shot: Shot) {
  const pid = await freshImport(page, 'table import');
  await previewTableFile(page, {
    name: 'council-spending.csv', mimeType: 'text/csv',
    buffer: Buffer.from('name,amount,approved,notes\nAda,1200,true,Council meeting\nMax,50,false,Planning hearing\nJo,75,true,Public records\n'),
  });
  await shot(page, 'Import workflow', 'table-check-rows', tableSource);
  await page.getByRole('region', { name: 'Check rows', exact: true })
    .getByRole('combobox', { name: /^Separator/ }).click();
  await expect(page.getByRole('listbox')).toBeVisible();
  await shot(page, 'Import workflow', 'table-separator-menu', tableSource);
  // Close the menu through its trigger; Escape currently also cancels the enclosing dialog.
  await page.getByRole('region', { name: 'Check rows', exact: true })
    .getByRole('combobox', { name: /^Separator/ }).click();
  await page.getByTestId('import-workspace-close').click();
  const discard = page.getByTestId('table-import-discard-dialog');
  await expect(discard).toBeVisible();
  await shot(page, 'Import workflow', 'table-discard-confirmation', 'src/components/importWorkspace/pdfBatch/DiscardImportConfirm.tsx');
  await discard.getByRole('button', { name: 'Keep working', exact: true }).click();
  await expect(discard).toHaveCount(0);
  await page.getByRole('button', { name: 'Columns →', exact: true }).click();
  await expect(page.getByRole('table', { name: 'Columns to import' })).toBeVisible();
  await shot(page, 'Import workflow', 'table-column-types', tableSource);
  await page.getByRole('button', { name: 'Continue →', exact: true }).click();
  const name = page.getByLabel('Sheet name', { exact: true });
  await name.fill('Council spending');
  const destinationBody = await page.locator('.table-import-destination .import-step-body').boundingBox();
  const destinationOptions = await page.locator('.table-import-destination .import-destination-options').boundingBox();
  expect(destinationBody!.width - destinationOptions!.width).toBe(48);
  await shot(page, 'Import workflow', 'table-destination', tableSource);
  await name.fill('');
  await expect(page.locator('.table-import-flow').getByRole('button', { name: /^Import(?: |$)/ })).toBeDisabled();
  await shot(page, 'Import workflow', 'table-destination-empty-name', tableSource);
  await name.fill('Council spending');
  const imported = await commitTableImport(page, pid);
  await expect.poll(async () => (await sheetData(page.request, pid, imported)).total).toBe(3);
  await openProject(page, pid, imported);
  await shot(page, 'Import workflow', 'table-import-result', 'src/components/ImportCsv.tsx; tableImport/TableImportFlow.tsx');

  await freshImport(page, 'pasted rows');
  await page.getByRole('button', { name: 'Paste rows', exact: true }).click();
  await page.getByRole('textbox', { name: 'Paste rows', exact: true }).fill('name\tamount\nAda\t1200\nMax\t50');
  await shot(page, 'Import workflow', 'table-paste-source', tableSource);

  await freshImport(page, 'Excel import');
  await page.getByRole('region', { name: 'Add data', exact: true })
    .locator('input[type="file"]').setInputFiles(path.resolve('tests/fixtures/import-people.xlsx'));
  await expect(page.getByRole('button', { name: 'Columns →', exact: true })).toBeEnabled();
  await shot(page, 'Import workflow', 'excel-check-rows', tableSource);
  await page.getByRole('region', { name: 'Check rows', exact: true })
    .getByRole('combobox', { name: /^Sheet/ }).click();
  await expect(page.getByRole('listbox')).toBeVisible();
  await shot(page, 'Import workflow', 'excel-worksheet-menu', tableSource);
  await page.getByRole('region', { name: 'Check rows', exact: true })
    .getByRole('combobox', { name: /^Sheet/ }).click();

  await page.getByRole('button', { name: 'Columns →', exact: true }).click();
  await page.getByRole('button', { name: 'Continue →', exact: true }).click();
  await expect(page.getByLabel('Sheet name', { exact: true })).toBeVisible();
  await shot(page, 'Import workflow', 'excel-destination', tableSource);

  await freshImport(page, 'multiple files');
  await page.getByTestId('import-mode-files').click();
  await page.getByTestId('import-file-input').setInputFiles([
    { name: 'meeting.pdf', mimeType: 'application/pdf', buffer: textPdf([['Council meeting', 'Budget approved: 1200 dollars']]) },
    { name: 'hearing.pdf', mimeType: 'application/pdf', buffer: textPdf([['Planning hearing', 'Public comment received']]) },
  ]);
  await expect(page.getByRole('button', { name: 'Check text →', exact: true })).toBeEnabled({ timeout: 30_000 });
  await shot(page, 'Import workflow', 'multiple-files-copied', batchSource);
  await page.getByRole('button', { name: 'Check text →', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Check text', exact: true })).toBeVisible();
  await expect(page.locator('.pdf-batch-preview').getByText(/Budget approved: 1200 dollars|Public comment received/)).toBeVisible();
  await expect(page.getByText('Loading choices…', { exact: true })).toHaveCount(0);
  await shot(page, 'Import workflow', 'multiple-files-check-text', batchSource);
  await page.getByRole('button', { name: 'Continue →', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Review destination', exact: true })).toBeVisible();
  await shot(page, 'Import workflow', 'multiple-files-destination', batchSource);
  await page.getByRole('button', { name: 'Search setup →', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Search setup', exact: true })).toBeVisible();
  await shot(page, 'Import workflow', 'multiple-files-search-setup', batchSource);

  await freshImport(page, 'split PDF');
  await page.getByTestId('import-mode-pdf_split').click();
  await page.getByTestId('pdf-packet-input').setInputFiles({
    name: 'council-packet.pdf', mimeType: 'application/pdf',
    buffer: textPdf([
      ['COUNCIL RECORD', 'Record 1', 'Budget discussion'],
      ['Supporting notes', 'Continuation of record 1'],
      ['COUNCIL RECORD', 'Record 2', 'Planning discussion'],
    ]),
  });
  await expect(page.getByRole('button', { name: 'Yes, use extracted text', exact: true })).toBeEnabled({ timeout: 30_000 });
  await shot(page, 'Import workflow', 'split-pdf-check-text', packetSource);
  await page.getByRole('button', { name: 'Yes, use extracted text', exact: true }).click();
  await expect(page.getByRole('group', { name: 'How to find document starts' })).toBeVisible();
  await shot(page, 'Import workflow', 'split-pdf-boundaries', packetSource);
  await page.getByRole('button', { name: 'Continue to import →', exact: true }).click();
  await expect(page.getByTestId('pdf-packet-flow')).toHaveAttribute('data-stage', 'import');
  await shot(page, 'Import workflow', 'split-pdf-destination', packetSource);

  await freshImport(page, 'feed source');
  await page.getByTestId('import-mode-feed').click();
  await page.getByTestId('source-url').fill('https://example.test/council/feed.xml');
  await page.getByTestId('source-name').fill('Council notices');
  await shot(page, 'Import workflow', 'feed-rss-options', 'src/components/ImportCsv.tsx');
  await page.getByTestId('source-kind').selectOption('api_list_dicts');
  await expect(page.getByTestId('source-api-list-path')).toBeVisible();
  await page.getByTestId('source-url').fill('https://example.test/api/notices');
  await page.getByTestId('source-api-list-path').fill('results');
  await shot(page, 'Import workflow', 'feed-api-options', 'src/components/ImportCsv.tsx');
  await page.getByTestId('source-kind').selectOption('youtube_playlist');
  await expect(page.getByTestId('source-youtube-max-items')).toBeVisible();
  await shot(page, 'Import workflow', 'feed-playlist-options', 'src/components/ImportCsv.tsx');
}
