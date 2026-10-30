import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import {
  commitTableImport, createProject, importCsv, openImportWorkspace, openProject,
  previewTableFile, sheetData, uniqueName,
} from '../helpers';
import { captureCheckpoint, publishCaptureBundle } from './capture';

test.use({ deviceScaleFactor: 2, viewport: { width: 1600, height: 1000 } });
test.skip(!process.env.FRISKET_DOCS_PROOF_DIR, 'Run through the isolated documentation capture harness');
test.beforeAll(() => {
  const root = path.resolve('..');
  const git = (...args: string[]) => execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
  expect(process.env.FRISKET_DOCS_REVISION).toBe(git('rev-parse', 'HEAD'));
  const changes = git('status', '--porcelain', '--untracked-files=no').split('\n').filter(Boolean);
  const overlay = process.env.FRISKET_DOCS_PACKAGE_OVERLAY === '1' ? ['M web/package.json'] : [];
  expect(changes.filter(change => !overlay.includes(change))).toEqual([]);
});

const metadata = () => ({ appRevision: process.env.FRISKET_DOCS_REVISION!, sample: { id: 'court-filings', revision: 1 } });
const output = (name: string) => path.join(process.env.FRISKET_DOCS_PROOF_DIR!, name);

async function openCourtDraft(page: Page) {
  await page.goto('/learn/court-filings?revision=1');
  await page.getByRole('button', { name: 'Open in Frisket', exact: true }).click();
  const flow = page.getByTestId('pdf-batch-flow');
  await expect(flow).toHaveAttribute('data-stage', 'check', { timeout: 90_000 });
  await flow.getByRole('radio', { name: /^None/ }).click();
  await expect(flow.getByRole('radio', { name: /^None/ })).toBeChecked();
  await expect(flow.getByText('Loading choices…')).toHaveCount(0, { timeout: 60_000 });
  return flow;
}

async function finishCourtImport(page: Page, flow: ReturnType<Page['getByTestId']>) {
  await flow.getByRole('button', { name: 'Continue →', exact: true }).click();
  await expect(flow).toHaveAttribute('data-stage', 'destination');
  await flow.getByRole('button', { name: 'Search setup →', exact: true }).click();
  await expect(flow).toHaveAttribute('data-stage', 'search');
  const semantic = flow.getByRole('checkbox', { name: /Search by meaning/ });
  if (await semantic.isChecked()) await semantic.uncheck();
  const importButton = flow.getByRole('button', { name: /^Import/ });
  await expect(importButton).toBeEnabled({ timeout: 60_000 });
  await importButton.click();
  await expect(flow).toHaveCount(0, { timeout: 90_000 });
  await expect(page.getByTestId('grid')).toBeVisible();
  const pid = new URL(page.url()).pathname.split('/')[2];
  const sheets = await (await page.request.get(`/api/projects/${pid}/sheets`)).json();
  const sheet = sheets.find((item: { name: string }) => item.name === 'Court filings');
  expect(sheet).toBeDefined();
  return { pid, sheet };
}

test('documentation: table import and the file and URL entry modes', async ({ page }) => {
  test.setTimeout(180_000);
  const csv = execFileSync(process.env.FRISKET_E2E_PYTHON!, ['-c',
    'from frisket.server.services.sample_project import sample_docket_csv; print(sample_docket_csv(), end="")'], { env: process.env });
  const pid = await createProject(page.request, uniqueName('docs-import-table'));
  const seed = await importCsv(page.request, pid, 'existing.csv', csv.toString());
  await openProject(page, pid, seed);
  await publishCaptureBundle(output('workflow-import-table'), metadata(), async stage => {
    const captures = [];
    await openImportWorkspace(page);
    const workspace = page.getByTestId('import-workspace');
    const add = page.getByRole('region', { name: 'Add data', exact: true });
    await expect(add.getByRole('button', { name: 'Choose file', exact: true })).toBeVisible();
    captures.push(await captureCheckpoint(page, { id: 'import-table-choose', root: workspace, annotations: [] }, stage));

    await add.getByRole('button', { name: 'Multiple files', exact: true }).click();
    await expect(page.getByTestId('import-source-files')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('import-file-picker')).toBeVisible();
    captures.push(await captureCheckpoint(page, { id: 'import-files-choose', root: workspace, annotations: [] }, stage));
    await page.getByTestId('import-source-urls').click();
    const urls = page.getByTestId('import-url-list');
    await urls.fill('https://example.org/filing.pdf');
    await expect(urls).toHaveValue('https://example.org/filing.pdf');
    await expect(page.getByTestId('import-url-submit')).toBeEnabled();
    captures.push(await captureCheckpoint(page, { id: 'import-urls-choose', root: workspace, annotations: [] }, stage));
    await page.getByTestId('import-mode-feed').click();
    await expect(page.getByTestId('source-kind')).toHaveValue('rss');
    await expect(page.getByTestId('source-url')).toHaveValue('');
    captures.push(await captureCheckpoint(page, { id: 'import-feed-choose', root: workspace, annotations: [] }, stage));
    await page.getByTestId('import-mode-csv').click();
    await previewTableFile(page, { name: 'expected.csv', mimeType: 'text/csv', buffer: csv });
    const rows = page.getByRole('region', { name: 'Check rows', exact: true });
    await expect(rows.getByRole('table')).toContainText('pdf_filename');
    await expect(rows.getByLabel('First row has column names')).toBeChecked();
    captures.push(await captureCheckpoint(page, { id: 'import-table-rows', root: workspace, annotations: [] }, stage));
    await page.getByRole('button', { name: 'Columns →', exact: true }).click();
    const columns = page.getByRole('table', { name: 'Columns to import' });
    await expect(columns).toContainText('case_number');
    await expect(columns).toContainText('Values as imported');
    captures.push(await captureCheckpoint(page, { id: 'import-table-columns', root: workspace, annotations: [] }, stage));
    await page.getByRole('button', { name: 'Continue →', exact: true }).click();
    const destination = page.getByRole('region', { name: 'Import', exact: true });
    await expect(destination.getByRole('radio', { name: 'New sheet' })).toBeChecked();
    await expect(destination.getByRole('button', { name: /^Import/ })).toBeEnabled();
    captures.push(await captureCheckpoint(page, { id: 'import-table-destination', root: workspace, annotations: [] }, stage));
    const sheet = await commitTableImport(page, pid);
    await expect.poll(async () => (await sheetData(page.request, pid, sheet)).total).toBe(6);
    await expect(page.getByTestId('grid')).toBeVisible();
    const result = await sheetData(page.request, pid, sheet);
    expect(result.columns.map(column => column.name)).toContain('case_number');
    await openProject(page, pid, sheet);
    await expect(page.getByTestId('sheet-stats')).toContainText('6 rows');
    await expect(page.getByText('Loading grid…')).toHaveCount(0, { timeout: 30_000 });
    captures.push(await captureCheckpoint(page, { id: 'import-table-result', root: page.getByTestId('workbench-region-mainView'), annotations: [] }, stage));
    return captures;
  });
});

test('documentation: import one row per PDF page', async ({ page }) => {
  test.setTimeout(180_000);
  const flow = await openCourtDraft(page);
  await publishCaptureBundle(output('workflow-import-pages'), metadata(), async stage => {
    await flow.getByRole('radio', { name: /^One row per page/ }).click();
    await expect(flow.getByRole('radio', { name: /^One row per page/ })).toBeChecked();
    const count = flow.getByText(/6 files → \d+ rows?/);
    await expect(count).toBeVisible();
    const match = (await count.innerText()).match(/→\s*(\d+) rows?/);
    expect(match).not.toBeNull();
    const captures = [await captureCheckpoint(page, { id: 'import-pdf-page-rows', root: flow, annotations: [] }, stage)];
    const { pid, sheet } = await finishCourtImport(page, flow);
    await expect.poll(async () => (await sheetData(page.request, pid, sheet.id)).total).toBe(Number(match![1]));
    const data = await sheetData(page.request, pid, sheet.id);
    expect(data.columns.map(column => column.name)).toContain('filename');
    captures.push(await captureCheckpoint(page, { id: 'import-pdf-page-result', root: page.getByTestId('grid'), maxHeight: 520, annotations: [] }, stage));
    return captures;
  });
});

test('documentation: split PDFs already in a sheet', async ({ page }) => {
  test.setTimeout(180_000);
  const flow = await openCourtDraft(page);
  await expect(flow.getByRole('radio', { name: /^Keep documents/ })).toBeChecked();
  const { pid, sheet } = await finishCourtImport(page, flow);
  await expect.poll(async () => (await sheetData(page.request, pid, sheet.id)).total).toBe(6);
  await publishCaptureBundle(output('workflow-split-existing-pdfs'), metadata(), async stage => {
    await page.getByTestId('ribbon-tab-ctx-pdf').click();
    await page.getByTestId('ribbon-command-split-pdfs').click();
    const dialog = page.getByRole('dialog', { name: 'Split PDFs', exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('radio', { name: /One row per page/ })).toBeChecked();
    await expect(dialog.getByRole('radio', { name: /New sheet/ })).toBeChecked();
    await expect(dialog.getByText('Checking text…')).toHaveCount(0, { timeout: 90_000 });
    await expect(dialog.getByRole('heading', { name: /Carry over columns/ })).toBeVisible();
    await expect(dialog.getByRole('button', { name: /^(Create|Check text)/ })).toBeEnabled({ timeout: 90_000 });
    return [await captureCheckpoint(page, { id: 'split-existing-pdf-setup', root: dialog, annotations: [] }, stage)];
  });
});
