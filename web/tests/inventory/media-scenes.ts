import { readFileSync } from 'node:fs';
import path from 'node:path';
import { expect, type Page } from '@playwright/test';
import type { HttpExtractionDocumentResponse } from '../../src/generated/openHttpContracts';
import { createProject, openProject, sheetColumns, sheetData, textPdf } from '../e2e/helpers';

type Shot = (page: Page, family: string, name: string, source: string) => Promise<void>;
const documentSource = 'src/workbench/DocumentView.tsx + DocumentReader.tsx';
const extractSource = 'src/workbench/extract/ExtractView.tsx';

async function importMedia(page: Page, project: string, sheetName: string, filename: string, mimeType: string, buffer: Buffer) {
  const response = await page.request.post(`/api/projects/${project}/import/files?sheet_name=${sheetName}`, {
    multipart: { files: { name: filename, mimeType, buffer } },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  const sheets: Array<{ id: number; name: string }> = await (await page.request.get(`/api/projects/${project}/sheets`)).json();
  const sheet = sheets.find(item => item.name === sheetName);
  expect(sheet).toBeDefined();
  return sheet!.id;
}

/** Real imported media and real local extraction. No provider/model calls or reader mocks. */
export async function captureMediaScenes(page: Page, shot: Shot) {
  const project = await createProject(page.request, 'Visual inventory documents');
  const pdfSheet = await importMedia(page, project, 'documents', 'inspection-report.pdf', 'application/pdf', textPdf([
    ['BUILDING INSPECTION', '', 'NAME: Morgan', 'CITY: Brooklyn', '', 'Inspector notes', 'The entrance and stairwell were inspected.', 'No urgent repairs are required.'],
    ['INSPECTION CONTINUED', '', 'Follow-up visit scheduled for November.'],
  ], { fontSize: 18, startY: 710, leading: 32 }));
  await openProject(page, project, pdfSheet);
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-pdf-page').first().locator('canvas')).toBeVisible();
  await page.getByTestId('document-options-button').click();
  await page.getByTestId('document-option-textlayer').check();
  await page.getByTestId('document-options-button').click();
  // The text layer is populated after PDF.js completes the canvas render.
  await expect(page.getByTestId('document-pdf-textlayer').first()).toContainText('BUILDING INSPECTION');
  await shot(page, 'Documents', 'PDF reader and document list', documentSource);
  await page.getByTestId('document-options-button').click();
  await expect(page.getByTestId('document-options')).toBeVisible();
  await shot(page, 'Documents', 'PDF view options', documentSource);
  await page.getByTestId('document-options-button').click();
  await page.getByTestId('document-show-alongside').click();
  await expect(page.getByTestId('document-alongside-pane')).toBeVisible();
  await shot(page, 'Documents', 'Show alongside column chooser', 'src/workbench/DocumentAlongsidePane.tsx');
  await page.getByRole('button', { name: 'Close alongside panel' }).click();

  // Persist the same layout an annotation session saves, using measured native-PDF
  // token boxes rather than screenshot coordinates or a mocked reader.
  const columns = await sheetColumns(page.request, project, pdfSheet);
  const media = columns.find(column => column.type === 'file')!;
  const rows = await sheetData(page.request, project, pdfSheet, 0, 1);
  const rowId = rows.rows[0].id;
  const geometryResponse = await page.request.get(`/api/projects/${project}/document-extraction/documents/${rowId}?sheet_id=${pdfSheet}&column_id=${media.id}`);
  expect(geometryResponse.ok(), await geometryResponse.text()).toBeTruthy();
  const geometry: HttpExtractionDocumentResponse = await geometryResponse.json();
  const documentPage = geometry.document.pages[0];
  function region(text: string) {
    const token = documentPage.tokens.find(item => item.text === text);
    expect(token, `Native PDF contains ${text}`).toBeDefined();
    const box = token!.box;
    return { page: documentPage.page, box: {
      x0: Math.max(0, box.x0 - .004), y0: Math.max(0, box.y0 - .004),
      x1: Math.min(1, box.x1 + .004), y1: Math.min(1, box.y1 + .004),
    } };
  }
  const savedResponse = await page.request.post(`/api/projects/${project}/document-extraction/templates`, { data: {
    sheet_id: pdfSheet, source: media.name, reference_row_id: rowId,
    draft: {
      // reference_page identifies a page-scoped source cell, not the page on
      // which annotations were drawn. A whole-PDF cell keeps it null.
      reference_blob_id: geometry.blob_id, reference_page: geometry.reference_page ?? null,
      reference_fingerprint: geometry.document.source_fingerprint,
      fields: [
        { id: 'name', name: 'Name', kind: 'key_value', key: region('NAME:'), value: region('Morgan'), section_id: null },
        { id: 'city', name: 'City', kind: 'key_value', key: region('CITY:'), value: region('Brooklyn'), section_id: null },
      ],
      sections: [], ignore_bands: [], expand_values: false, look_every_page: true,
      continue_across_pages: false, pending: null,
    },
  } });
  expect(savedResponse.ok(), await savedResponse.text()).toBeTruthy();
  const saved: { id: number } = await savedResponse.json();
  const selected = await page.request.post(`/api/projects/${project}/document-extraction/selection`, {
    data: { sheet_id: pdfSheet, source: media.name, layout_id: saved.id },
  });
  expect(selected.ok(), await selected.text()).toBeTruthy();
  await page.getByTestId('view-switch-extract').click();
  await expect(page.getByLabel('Column name for Name', { exact: true })).toBeEnabled();
  await expect(page.getByTestId('extract-preview-button')).toBeEnabled();
  await shot(page, 'Extract', 'PDF layout with key value fields', extractSource);
  await page.getByTestId('extract-scope-selector').click();
  await expect(page.getByTestId('extract-scope-selector-menu')).toBeVisible();
  await shot(page, 'Extract', 'Apply scope menu', extractSource);
  await page.getByTestId('extract-scope-selector').click();
  await page.getByTestId('extract-options-button').click();
  await shot(page, 'Extract', 'Layout extraction options', extractSource);
  await page.getByTestId('extract-options-button').click();
  await page.getByTestId('extract-preview-button').click();
  await expect(page.getByText('Extracting sample documents…', { exact: true })).toHaveCount(0);
  await expect(page.getByText(/1-document sample/)).toBeVisible();
  await shot(page, 'Extract', 'Populated local extraction preview', extractSource);
  await page.getByTestId('extract-new-sheet').click();
  await expect(page.getByRole('dialog', { name: 'Extract to new sheet' })).toBeVisible();
  await shot(page, 'Extract', 'Extract to new sheet name dialog', 'src/workbench/extract/ExtractSheetNameDialog.tsx');
  await page.getByRole('dialog', { name: 'Extract to new sheet' }).getByRole('button', { name: 'Cancel', exact: true }).click();

  for (const kind of ['audio', 'video'] as const) {
    const extension = kind === 'audio' ? 'wav' : 'mp4';
    const sheet = await importMedia(page, project, kind, `interview.${extension}`, `${kind}/${kind === 'audio' ? 'wav' : 'mp4'}`,
      readFileSync(path.resolve('tests/fixtures/media', `tiny-${kind}.${extension}`)));
    await openProject(page, project, sheet);
    await page.getByTestId('view-switch-document').click();
    await expect(page.getByTestId(`document-${kind}`)).toBeVisible();
    await expect.poll(() => page.getByTestId(`document-${kind}`).evaluate(
      element => (element.matches('audio, video') ? element as HTMLMediaElement
        : element.querySelector<HTMLMediaElement>('audio, video'))?.readyState ?? 0,
    )).toBeGreaterThanOrEqual(2);
    await shot(page, 'Documents', `${kind} document and playback controls`, documentSource);
    if (kind === 'video') {
      await page.getByTestId('document-video-fit-toggle').click();
      await expect(page.getByTestId('document-video')).toHaveAttribute('data-video-fit', 'fit-height');
      await shot(page, 'Documents', 'Video fit to height', documentSource);
    }
    await page.getByTestId('document-options-button').click();
    await expect(page.getByTestId('document-options')).toBeVisible();
    await shot(page, 'Documents', `${kind} document options`, documentSource);
    await page.getByTestId('document-options-button').click();
    // The PDF Extract workspace is not an audio/video editor. Capture the actual
    // supported temporal action form without executing an extraction/model call.
    await page.goto(`/p/${project}/s/${sheet}/action/temporal.extract_range`);
    await expect(page.getByTestId('action-drawer')).toBeVisible();
    await expect(page.getByTestId('action-form-title')).toBeVisible();
    await shot(page, 'Extract', `${kind} temporal extract action form`, 'src/actions + temporal.extract_range (not the PDF Extract pane)');
  }
}
