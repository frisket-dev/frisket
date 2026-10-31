import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, importCsv, openPalette, openProject, textPdf } from '../helpers';
import { captureCheckpoint, captureRibbon, publishCaptureBundle } from './capture';

test.use({ deviceScaleFactor: 2, viewport: { width: 1600, height: 1000 } });
test.skip(!process.env.FRISKET_DOCS_PROOF_DIR, 'Run through the isolated documentation capture harness');
test.beforeAll(() => {
  const git = (...args: string[]) => execFileSync('git', args, { cwd: path.resolve('..'), encoding: 'utf8' }).trim();
  expect(process.env.FRISKET_DOCS_REVISION).toBe(git('rev-parse', 'HEAD'));
  const overlay = process.env.FRISKET_DOCS_PACKAGE_OVERLAY === '1' ? ['M web/package.json'] : [];
  expect(git('status', '--porcelain', '--untracked-files=no').split('\n').filter(Boolean).filter(change => !overlay.includes(change))).toEqual([]);
});

test('documentation: compare two local OCR settings on one real page', async ({ page }) => {
  test.setTimeout(180_000);
  const pid = await createProject(page.request, 'OCR comparison documentation');
  const sheet = await importCsv(page.request, pid, 'seed.csv', 'name\nExample\n');
  await openProject(page, pid, sheet);
  await publishCaptureBundle(path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'ocr-local-comparison'), {
    appRevision: process.env.FRISKET_DOCS_REVISION!, fixture: { id: 'ocr-local-fixture', revision: 1 },
  }, async stage => {
    await page.getByTestId('ribbon-tab-media').click();
    await page.getByTestId('ribbon-command-ocr-compare').click();
    const compare = page.getByTestId('ocr-compare-tab');
    await expect(compare).toBeVisible();
    const rapid = compare.locator('[data-testid="ocr-compare-variant-chip"][data-engine-id="rapidocr"]').first();
    await expect(rapid).toBeVisible({ timeout: 60_000 });
    for (const other of await compare.locator('[data-testid="ocr-compare-variant-chip"]:not([data-engine-id="rapidocr"])').all()) {
      await other.getByTestId('ocr-compare-variant-remove').click();
    }
    await compare.getByTestId('ocr-compare-file-input').setInputFiles({ name: 'harbor.pdf', mimeType: 'application/pdf', buffer: textPdf([
      ['HARBOR REPORT', 'The bluewharf contract covers dredging.'],
    ], { fontSize: 18, leading: 24 }) });
    await rapid.getByTestId('ocr-compare-variant-gear').click();
    const options = compare.getByTestId('ocr-compare-configure');
    await expect(options).toBeVisible();
    await options.getByTestId('ocr-compare-duplicate').click();
    await expect(compare.locator('[data-testid="ocr-compare-variant-chip"][data-engine-id="rapidocr"]')).toHaveCount(2);
    const dpi = compare.getByTestId('ocr-compare-dpi-stepper');
    await dpi.fill('150');
    await expect(dpi).toHaveValue('150');
    await page.keyboard.press('Escape');
    await expect(compare.getByTestId('ocr-compare-engine-column')).toHaveCount(2, { timeout: 90_000 });
    await expect(compare.getByTestId('ocr-compare-run')).toBeEnabled({ timeout: 60_000 });
    const setup = await captureCheckpoint(page, { id: 'ocr-compare-ready', root: compare, maxHeight: 220, annotations: [] }, stage);
    await compare.getByTestId('ocr-compare-run').click();
    const results = compare.locator('[data-testid="ocr-compare-engine-column"][data-run-status="done"]');
    await expect(results).toHaveCount(2, { timeout: 120_000 });
    await compare.getByTestId('ocr-compare-mode-survey').click();
    await expect(results.first()).toContainText(/harbor|bluewharf/i);
    await expect(results.nth(1)).toContainText(/harbor|bluewharf/i);
    return [setup, await captureCheckpoint(page, { id: 'ocr-compare-results', root: compare, maxHeight: 280, annotations: [] }, stage)];
  });
});

test('documentation: native PDF search preparation and OCR comparison setup', async ({ page }) => {
  test.setTimeout(180_000);
  const pid = await createProject(page.request, 'PDF search preparation documentation');
  const textSheet = await importCsv(page.request, pid, 'dispatches.csv', 'story\nHarbor meeting\n');
  const settings = await page.request.patch(`/api/projects/${pid}/settings`, { data: { retrieval: { prepare_new_files_automatically: false } } });
  expect(settings.ok()).toBeTruthy();
  const pdf = textPdf([['Harbor report', 'The bluewharf contract covers dredging.'], ['Notes', 'The council requested a revised plan.']]);
  const imported = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=Documents`, {
    multipart: { files: { name: 'harbor.pdf', mimeType: 'application/pdf', buffer: pdf } },
  });
  expect(imported.ok()).toBeTruthy();
  const { sheet_id: documentSheet } = await imported.json() as { sheet_id: number };
  await openProject(page, pid, textSheet);
  const output = path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'media-workflows');
  await publishCaptureBundle(output, { appRevision: process.env.FRISKET_DOCS_REVISION!, fixture: { id: 'media-workflows-fixture', revision: 1 } }, async stage => {
    const captures = [];
    const palette = await openPalette(page);
    await palette.getByTestId('search-mode-select').selectOption('lexical');
    await palette.getByTestId('command-palette-input').fill('bluewharf');
    const gap = palette.getByTestId('search-preparation-gap');
    await expect(gap.getByTestId('search-preparation-gap-action')).toContainText('Documents');
    captures.push(await captureCheckpoint(page, { id: 'search-prepare-gap', root: palette, annotations: [] }, stage));
    const planResponse = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/projects/${pid}/search-preparation`);
    await gap.getByTestId('search-preparation-gap-action').click();
    const response = await planResponse;
    expect(response.ok()).toBeTruthy();
    expect(response.request().postDataJSON()).toEqual({ sheet_id: documentSheet });
    const plan = await response.json() as { id: string; status: string };
    expect(plan.status).toBe('needs_consent');
    const receipt = page.getByTestId('search-preparation-receipt');
    await expect(receipt).toBeVisible();
    const preview = receipt.getByTestId('search-preparation-text-preview');
    await expect(preview.getByText('bluewharf', { exact: false })).toBeVisible();
    const thumbnail = preview.locator('img');
    if (await thumbnail.count()) {
      await expect.poll(() => thumbnail.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true);
    }
    const semantic = receipt.getByTestId('search-preparation-semantic-model');
    if (await semantic.isVisible().catch(() => false) && await semantic.isChecked()) await semantic.uncheck();
    const prepare = receipt.getByTestId('search-preparation-decision-prepare');
    await expect(prepare).toHaveText('Make searchable');
    await expect(prepare).toBeEnabled();
    await receipt.getByRole('heading', { name: 'Make these files searchable' }).scrollIntoViewIfNeeded();
    captures.push(await captureCheckpoint(page, { id: 'search-prepare-plan', root: receipt, annotations: [] }, stage));
    const decision = page.waitForResponse(result => result.request().method() === 'POST' && new URL(result.url()).pathname === `/api/projects/${pid}/search-preparation/${plan.id}/decision`);
    await prepare.click();
    const admitted = await decision;
    expect(admitted.ok()).toBeTruthy();
    expect(admitted.request().postDataJSON().selected_setup_refs).toEqual([]);
    await expect.poll(async () => (await (await page.request.get(`/api/projects/${pid}/search-preparation/${plan.id}`)).json()).status, { timeout: 60_000 }).toBe('complete');
    const closePalette = page.getByRole('button', { name: 'Close command palette' });
    if (await closePalette.isVisible()) await closePalette.click();
    const searched = await openPalette(page);
    await searched.getByTestId('search-mode-select').selectOption('lexical');
    await searched.getByTestId('command-palette-input').fill('bluewharf');
    await expect(searched.getByTestId('search-hit').filter({ hasText: 'bluewharf' }).first()).toBeVisible({ timeout: 30_000 });
    captures.push(await captureCheckpoint(page, { id: 'search-prepare-result', root: searched, annotations: [] }, stage));
    await searched.getByRole('button', { name: 'Close command palette' }).click();
    await page.getByTestId('ribbon-tab-media').click();
    const ocr = page.getByTestId('ribbon-command-ocr-compare');
    await expect(ocr).toBeVisible();
    captures.push(await captureRibbon(page, 'ocr-compare-entry', page.getByTestId('ribbon-tab-media'), ocr, stage));
    await ocr.click();
    const compare = page.getByTestId('ocr-compare-tab');
    await expect(compare).toBeVisible();
    await compare.getByTestId('ocr-compare-file-input').setInputFiles({ name: 'harbor.pdf', mimeType: 'application/pdf', buffer: pdf });
    await expect(compare).toContainText('harbor.pdf');
    await expect(compare.getByText('Loading models…')).toHaveCount(0, { timeout: 60_000 });
    await expect(compare.getByText('Loading choices…')).toHaveCount(0, { timeout: 60_000 });
    captures.push(await captureCheckpoint(page, { id: 'ocr-compare-setup', root: compare, annotations: [] }, stage));
    return captures;
  });
});
