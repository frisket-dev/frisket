import { appendFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import type { HttpExtractionDocumentResponse } from '../../src/generated/openHttpContracts';
import { createProject, importCsv, openProject, sheetColumns, sheetData, textPdf } from '../e2e/helpers';

const output = path.resolve(process.env.FRISKET_INVENTORY_OUTPUT ?? 'screenshots/editor-details');

let errors: string[] = [];
test.beforeEach(async ({ page, context }) => {
  errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', async route => {
    if (new URL(route.request().url()).origin === process.env.FRISKET_INVENTORY_APP_URL) return route.continue();
    await route.abort();
    throw new Error(`Unexpected external editor inventory request: ${route.request().url()}`);
  });
});
test.afterEach(() => expect(errors).toEqual([]));

test('repeated extraction controls fit at normal and narrow widths', async ({ page }) => {
  const pid = await createProject(page.request, 'Repeated extraction inventory');
  const imported = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=documents`, {
    multipart: { files: { name: 'inspection-records.pdf', mimeType: 'application/pdf', buffer: textPdf([
      ['INSPECTION RECORDS', '', 'NAME: Morgan', 'CITY: Brooklyn', '', 'NAME: Alex', 'CITY: Queens'],
      ['INSPECTION RECORDS', '', 'NAME: Sam', 'CITY: Manhattan'],
    ], { fontSize: 18, startY: 710, leading: 32 }) } },
  });
  expect(imported.ok(), await imported.text()).toBeTruthy();
  const sheets: Array<{ id: number }> = await (await page.request.get(`/api/projects/${pid}/sheets`)).json();
  const sid = sheets[0].id;
  const columns = await sheetColumns(page.request, pid, sid);
  const media = columns.find(column => column.type === 'file')!;
  const rows = await sheetData(page.request, pid, sid, 0, 1);
  const rowId = rows.rows[0].id;
  const response = await page.request.get(`/api/projects/${pid}/document-extraction/documents/${rowId}?sheet_id=${sid}&column_id=${media.id}`);
  expect(response.ok(), await response.text()).toBeTruthy();
  const geometry: HttpExtractionDocumentResponse = await response.json();
  const firstPage = geometry.document.pages[0];
  const region = (text: string) => {
    const token = firstPage.tokens.find(item => item.text === text)!;
    expect(token, `Native PDF contains ${text}`).toBeDefined();
    return { page: firstPage.page, box: {
      x0: token.box.x0 - .004, x1: token.box.x1 + .004,
      y0: token.box.y0 - .004, y1: token.box.y1 + .004,
    } };
  };
  const name = region('NAME:');
  const city = region('Brooklyn');
  const rest = region('Alex');
  const saved = await page.request.post(`/api/projects/${pid}/document-extraction/templates`, { data: {
    sheet_id: sid, source: media.name, reference_row_id: rowId,
    draft: {
      reference_blob_id: geometry.blob_id, reference_page: geometry.reference_page ?? null,
      reference_fingerprint: geometry.document.source_fingerprint,
      fields: [
        { id: 'name', name: 'Name', kind: 'key_value', key: name, value: region('Morgan'), section_id: 'records' },
        { id: 'city', name: 'City', kind: 'key_value', key: region('CITY:'), value: city, section_id: 'records' },
      ],
      sections: [{ id: 'records', name: 'Inspection record',
        first: { start: { page: 1, y: name.box.y0 - .01 }, end: { page: 1, y: city.box.y1 + .01 } },
        rest: { start: { page: 1, y: rest.box.y0 - .01 }, end: { page: 2, y: .9 } },
      }],
      ignore_bands: [{ box: { x0: 0, y0: 0, x1: 1, y1: name.box.y0 - .02 } }],
      expand_values: false, look_every_page: true, continue_across_pages: false, pending: null,
    },
  } });
  expect(saved.ok(), await saved.text()).toBeTruthy();
  const selected = await page.request.post(`/api/projects/${pid}/document-extraction/selection`, {
    data: { sheet_id: sid, source: media.name, layout_id: (await saved.json()).id },
  });
  expect(selected.ok(), await selected.text()).toBeTruthy();
  await openProject(page, pid, sid);
  await page.getByTestId('view-switch-extract').click();
  const fields = page.getByRole('complementary', { name: 'Extraction fields' });
  await expect(fields.getByLabel('Repeated section name')).toHaveValue('Inspection record');
  await expect(fields.getByRole('button', { name: 'Ignored region 1', exact: true })).toBeVisible();
  await expect(page.getByTestId('document-pdf-page').first().locator('canvas, img')).toBeVisible();
  mkdirSync(output, { recursive: true });
  const widths: Array<{ viewport: number; first: number; remaining: number }> = [];
  for (const width of [1440, 1000]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.evaluate(() => { document.documentElement.dataset.frisketTheme = 'light'; });
    await page.evaluate(() => document.fonts.ready);
    const name = `Repeated section and ignored region ${width === 1440 ? 'normal' : 'narrow'}`;
    const file = `extract-repeated-${width}-light.png`;
    const detail = `extract-repeated-${width}-light-detail.png`;
    await page.screenshot({ path: path.join(output, file), animations: 'disabled' });
    await fields.screenshot({ path: path.join(output, detail), animations: 'disabled' });
    appendFileSync(path.join(output, 'captures.ndjson'), `${JSON.stringify({ family: 'Extract', name,
      source: 'src/workbench/extract/ExtractFields.tsx (real PDF; persisted repeated layout)',
      file, detail, theme: 'light' })}\n`);
    widths.push({ viewport: width,
      first: (await fields.getByLabel('First record ends on page', { exact: true }).boundingBox())!.width,
      remaining: (await fields.getByLabel('Remaining records end on page', { exact: true }).boundingBox())!.width,
    });
    await fields.getByRole('button', { name: 'Delete ignored region 1', exact: true }).scrollIntoViewIfNeeded();
    const ignoredFile = `extract-repeated-ignored-${width}-light.png`;
    const ignoredDetail = `extract-repeated-ignored-${width}-light-detail.png`;
    await page.screenshot({ path: path.join(output, ignoredFile), animations: 'disabled' });
    await fields.screenshot({ path: path.join(output, ignoredDetail), animations: 'disabled' });
    appendFileSync(path.join(output, 'captures.ndjson'), `${JSON.stringify({ family: 'Extract',
      name: `Ignored region controls ${width === 1440 ? 'normal' : 'narrow'}`,
      source: 'src/workbench/extract/ExtractFields.tsx (same persisted layout; fields pane scrolled)',
      file: ignoredFile, detail: ignoredDetail, theme: 'light' })}\n`);
    await fields.evaluate(element => { element.scrollTop = 0; });
  }
  console.info('Repeated section numeric control widths:', JSON.stringify(widths));
  for (const size of widths) {
    expect(size.first, `${size.viewport}px first-record number field`).toBeGreaterThanOrEqual(48);
    expect(size.remaining, `${size.viewport}px remaining-records number field`).toBeGreaterThanOrEqual(48);
  }
});

test('populated combine groups and add-to menu', async ({ page }) => {
  const pid = await createProject(page.request, 'Combine editor inventory');
  const sid = await importCsv(page.request, pid, 'boroughs.csv', 'borough\nBrooklyn\nBK\nQueens\nQNS\n');
  await page.goto(`/p/${pid}/s/${sid}/action/resolve.combine`);
  const values = page.getByTestId('resolve-value-row');
  await expect(values).toHaveCount(4);
  await values.filter({ hasText: /^Brooklyn/ }).click();
  await values.filter({ hasText: /^BK/ }).click({ modifiers: ['Control'] });
  await page.getByTestId('resolve-combine-new-bucket').click();
  const group = page.getByTestId('resolve-combine-bucket');
  await expect(group.getByTestId('resolve-group-member')).toHaveCount(2);
  await group.getByTestId('resolve-group-canonical-input').fill('Brooklyn');
  await group.getByTestId('resolve-group-canonical-input').press('Tab');
  await values.first().click();
  mkdirSync(output, { recursive: true });
  for (const width of [1440, 1000]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.evaluate(() => { document.documentElement.dataset.frisketTheme = 'light'; });
    await page.getByTestId('resolve-combine-add-to').click();
    await expect(page.getByTestId('resolve-combine-add-to-menu')).toBeVisible();
    const file = `actions-combine-populated-${width}-light.png`;
    const detail = `actions-combine-populated-${width}-light-detail.png`;
    await page.screenshot({ path: path.join(output, file), animations: 'disabled' });
    await page.getByTestId('action-drawer').screenshot({ path: path.join(output, detail), animations: 'disabled' });
    appendFileSync(path.join(output, 'captures.ndjson'), `${JSON.stringify({ family: 'Actions',
      name: `Combine populated group ${width === 1440 ? 'normal' : 'narrow'}`,
      source: 'src/components/resolve/CombineForm.tsx; GroupCard.tsx (real values; no action executed)',
      file, detail, theme: 'light' })}\n`);
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('resolve-combine-add-to-menu')).toHaveCount(0);
    await expect(page.getByTestId('resolve-combine-add-to')).toBeFocused();
    await expect(group).toBeVisible();
  }
});
