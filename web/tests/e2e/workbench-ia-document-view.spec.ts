// Workbench IA — Document view: a first-class media-reading sheet VIEW beside
// Grid/Map/Gallery/Graph.
//
// Bound here: (1) '▣ Document' joins the SAME view switcher,
// data-keyed to a media/file column, and REPLACES the grid region (not a split);
// switching views clears view-local row selection without changing sort/filter. (2) Left doc
// list = the collection in the sheet's CURRENT order. (3) Center reader: pdf.js
// continuous pages, self-hosted worker (NO CDN). (4) Two nexts: list click/keys
// change the DOCUMENT, header ‹ › change the PAGE. (5) Document focus stays local
// to the reader; it never writes SelectedGridRows. (6) The row Detail pane docks
// right unchanged → three columns. (7) ⚙ view options. (8) row without media →
// 'no document' state, not an error.
//
// Seeds a REAL text-layer PDF through the real /import/files blob path (the same
// path media-to-markdown-v1-execution.spec.ts uses); the inline textPdf()
// generator writes genuine BT/Tj/ET text streams so pdf.js extracts a text
// layer and reports a real page count (never fabricated).

import { expect, test, type Page } from '@playwright/test';
import {
  addRow,
  createProject,
  importCsv,
  listSheets,
  openCellDrawer,
  openAction,
  openProject,
  patchColumn,
  projectIdByName,
  runAndWait,
  selectRow,
  sheetColumns,
  sheetData,
  textPdf,
  uniqueName,
} from './helpers';

// textPdf hand-writes a valid multi-page %PDF-1.4 with a real extractable text
// layer (one Page + Contents stream per page) — the JS analogue of reportlab's
// _make_pdf — so pdf.js parses a real numPages/text layer (never fabricated).
// Shared with document-view-screenshots.spec.ts (task
// e2e-helper-consolidation-v1); this file's calls use the builder's defaults
// (fontSize 12, startX 50, startY 742, leading 14), its original values.

interface DocSheet {
  pid: string;
  sheetId: number;
  mediaColumnId: number;
  firstRowId: number;
}

/** Create a project and import a real 2-page text-layer PDF through the blob
 *  path, producing a sheet with a `file` (media) column. */
async function seedDocSheet(page: Page, opts: { extraDocs?: boolean } = {}): Promise<DocSheet> {
  const pid = await createProject(page.request, uniqueName('document-view'));
  const importRes = await page.request.post(
    `/api/projects/${pid}/import/files?sheet_name=documents`,
    {
      multipart: {
        files: {
          name: 'annual-report.pdf',
          mimeType: 'application/pdf',
          buffer: textPdf([
            ['Annual Report', 'Page one of the filing.'],
            ['Appendix', 'Page two of the filing.'],
          ]),
        },
      },
    },
  );
  expect(importRes.ok()).toBeTruthy();
  const sheets = (await (await page.request.get(`/api/projects/${pid}/sheets`)).json()) as Array<{
    id: number;
    name: string;
  }>;
  const sheet = sheets.find((candidate) => candidate.name === 'documents')!;
  expect(sheet).toBeTruthy();
  const columns = await sheetColumns(page.request, pid, sheet.id);
  const mediaColumn = columns.find((column) =>
    ['file', 'image', 'video'].includes(column.type),
  )!;
  expect(mediaColumn).toBeTruthy();
  const firstPage = await sheetData(page.request, pid, sheet.id, 0, 5);
  const firstRow = firstPage.rows[0];
  const firstCell = firstRow.cells[String(mediaColumn.id)] as Record<string, unknown>;

  if (opts.extraDocs) {
    // A second real document (same blob is fine — distinct filename → distinct
    // list title), then a THIRD row with NO media for the empty-state edge case.
    await addRow(page.request, pid, sheet.id, {
      [mediaColumn.name]: { ...firstCell, filename: 'supplement.pdf' },
      filename: 'supplement.pdf',
    });
    await addRow(page.request, pid, sheet.id, {});
  }
  return { pid, sheetId: sheet.id, mediaColumnId: mediaColumn.id, firstRowId: firstRow.id };
}

test('the Document segment is data-keyed to a media column and replaces the grid with a list + reader', async ({
  page,
}) => {
  // A plain CSV sheet (no media column) must NOT surface the segment.
  const plainPid = await createProject(page.request, uniqueName('document-view-plain'));
  const plainSheet = await importCsv(page.request, plainPid, 'cities.csv', 'city\nParis\nBerlin\n');
  await openProject(page, plainPid, plainSheet);
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('view-switch-document')).toHaveCount(0);

  // A sheet WITH a real PDF (file) column surfaces the segment.
  const { pid, sheetId } = await seedDocSheet(page);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('view-switch-document')).toBeVisible();
  await expect(page.getByTestId('view-switch-document')).toHaveAttribute('aria-pressed', 'false');

  // Clicking Document REPLACES the grid (not a split): the grid canvas is gone,
  // and there is no split chrome — the reader gets the whole Work region.
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('view-switch-document')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('document-view')).toBeVisible();
  await expect(page.getByTestId('workbench-mainView-split')).toHaveCount(0);
  await expect(page.getByTestId('grid')).toHaveCount(0);

  // Left list + center reader; the reader names the doc + a PDF media chip.
  await expect(page.getByTestId('document-list')).toBeVisible();
  await expect(page.getByTestId('document-list-item')).toHaveCount(1);
  await expect(page.getByTestId('document-reader')).toBeVisible();
  await expect(page.getByTestId('document-reader-chip')).toContainText(/pdf/i);

  // Leaving to Grid and returning keeps the Document view (chrome-state place).
  await page.getByTestId('view-switch-grid').click();
  await expect(page.getByTestId('grid')).toBeVisible();
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-view')).toBeVisible();
});

test('the reader loads pdf.js self-hosted (no CDN) and reports a real page count; header ‹ › page nav moves within the current document', async ({
  page,
}) => {
  const cdnRequests: string[] = [];
  page.on('request', (req) => {
    const host = new URL(req.url()).host;
    if (/cdnjs|unpkg|jsdelivr|cloudflare|googleapis|mozilla\.github/i.test(host)) {
      cdnRequests.push(req.url());
    }
  });

  const { pid, sheetId } = await seedDocSheet(page);
  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-view')).toBeVisible();

  // The 2-page PDF's real page count comes from pdf.js parsing the blob — never
  // fabricated. data-page-count settles to '2' once the document loads.
  const pdf = page.getByTestId('document-pdf');
  await expect(pdf).toHaveAttribute('data-page-count', '2', { timeout: 20_000 });

  // Page nav is the CENTER header's job (distinct from the left list's document
  // nav). At page 1 prev is disabled; next advances the indicator.
  await expect(page.getByTestId('document-page-indicator')).toContainText('1 / 2');
  await expect(page.getByTestId('document-page-prev')).toBeDisabled();
  await page.getByTestId('document-page-next').click();
  await expect(page.getByTestId('document-page-indicator')).toContainText('2 / 2');
  await expect(page.getByTestId('document-page-next')).toBeDisabled();

  // pdf.js and its worker were served same-origin — nothing loaded from a CDN.
  expect(cdnRequests).toEqual([]);
});

test('Document view keeps the source PDF beside a selected derived markdown value as documents change', async ({
  page,
}) => {
  const { pid, sheetId } = await seedDocSheet(page, { extraDocs: true });
  await runAndWait(page.request, pid, {
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { template: { text: '## {{filename}}\n\nDerived filing text' } },
    output_names: { rendered: 'filing_text' },
    idempotency_key: `document-view-template-${pid}`,
  });
  const filingText = (await sheetColumns(page.request, pid, sheetId)).find(
    (column) => column.name === 'filing_text',
  );
  expect(filingText?.ai_generated).toBe(true);
  expect(filingText).toBeTruthy();
  await patchColumn(page.request, pid, filingText!.id, { format: 'markdown' });

  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-document').click();
  const pdf = page.getByTestId('document-pdf');
  await expect(pdf).toBeVisible();
  await expect(pdf).toHaveAttribute('data-page-count', '2');

  const comparison = page.getByTestId('document-derived-pane');
  const picker = page.getByTestId('document-derived-column-select');
  await expect(comparison).toBeVisible();
  await picker.selectOption(String(filingText!.id));
  await expect(comparison.getByTestId('markdown-value')).toContainText('annual-report.pdf');
  await expect(pdf).toBeVisible();
  await expect(page.getByTestId('grid')).toHaveCount(0);
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);

  const documents = page.getByTestId('document-list-item');
  await documents.nth(1).click();
  await expect(documents.nth(1)).toHaveAttribute('data-active', 'true');
  await expect(pdf).toBeVisible();
  await expect(comparison.getByTestId('markdown-value')).toContainText('supplement.pdf');
});

test('selection syncs BOTH ways through SelectedGridRows; the doc list follows the sheet order', async ({
  page,
}) => {
  const { pid, sheetId } = await seedDocSheet(page, { extraDocs: true });
  await openProject(page, pid, sheetId);

  // Grid → Document: selecting a grid row and switching carries the selection,
  // so the reader opens that document (the highlighted list item IS the row).
  await selectRow(page, 1);
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-view')).toBeVisible();
  await expect(page.getByTestId('document-selection')).toHaveAttribute('data-selection-count', '1');
  await expect(page.getByTestId('document-list-item').nth(1)).toHaveAttribute('data-active', 'true');

  // Document → Grid: clicking a different list item writes the SAME
  // SelectedGridRows state; switching back, the grid toolbar reflects it.
  await page.getByTestId('document-list-item').nth(0).click();
  await expect(page.getByTestId('document-list-item').nth(0)).toHaveAttribute('data-active', 'true');
  await expect(page.getByTestId('document-selection')).toHaveAttribute('data-selection-count', '1');
  await page.getByTestId('view-switch-grid').click();
  await expect(page.getByTestId('delete-rows-button')).toHaveAttribute(
    'title',
    /Delete 1 selected row/,
  );
});

test('a row without media shows the no-document state, not an error; opening the row Detail yields the three-column layout', async ({
  page,
}) => {
  const { pid, sheetId } = await seedDocSheet(page, { extraDocs: true });
  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-view')).toBeVisible();
  await expect(page.getByTestId('document-list-item')).toHaveCount(3);

  // The media-bearing doc: reader shows the PDF, not the empty prompt.
  await page.getByTestId('document-list-item').nth(0).click();
  await expect(page.getByTestId('document-pdf')).toBeVisible();
  await expect(page.getByTestId('document-empty')).toHaveCount(0);

  // The row without media: a calm 'no document' prompt (NOT an error surface).
  const emptyItem = page.getByTestId('document-list-item').nth(2);
  await expect(emptyItem).toHaveAttribute('data-has-media', 'false');
  await emptyItem.click();
  await expect(page.getByTestId('document-empty')).toBeVisible();
  await expect(page.getByTestId('document-pdf')).toHaveCount(0);

  // Open the row Detail from the reader → the resident Detail column docks right
  // (three columns: list | reader | Detail), the same InspectDetailColumn.
  await page.getByTestId('document-list-item').nth(0).click();
  await page.getByTestId('document-open-detail').click();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('document-view')).toBeVisible();
});

test('the row Detail media preview offers an Open in Document view entry point', async ({
  page,
}) => {
  const { pid, sheetId } = await seedDocSheet(page);
  await openProject(page, pid, sheetId);
  const columns = await sheetColumns(page.request, pid, sheetId);

  // Open the row Detail from the grid. columns[0] ('filename') is a plain
  // non-AI text column (now in-place editable — grid-in-place-edit-v1), so
  // the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, columns[0].name, 0);
  await expect(page.getByTestId('row-drawer')).toBeVisible();

  // The Detail carries the Document-view entry point; clicking it switches the
  // work view to Document, focused on this row's media.
  const openInDocument = page.getByTestId('row-open-in-document-view');
  await expect(openInDocument).toBeVisible();
  await openInDocument.click();
  await expect(page.getByTestId('document-view')).toBeVisible();
  await expect(page.getByTestId('view-switch-document')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('document-reader-chip')).toContainText(/pdf/i);
});

test('document browsing does not become a hidden Run-selected scope', async ({ page }) => {
  const { pid, sheetId } = await seedDocSheet(page, { extraDocs: true });
  await openProject(page, pid, sheetId);

  await selectRow(page, 0);
  await page.getByTestId('view-switch-document').click();
  await page.getByTestId('document-list-item').nth(1).click();
  await page.getByTestId('view-switch-grid').click();

  await openAction(page, 'media.to_markdown');
  const drawer = page.getByTestId('action-drawer');
  await expect(drawer.getByTestId('row-scope-summary')).toContainText('All');
  await drawer.getByTestId('run-scope-menu-button').click();
  await expect(drawer.getByTestId('row-scope-selected')).toBeDisabled();
});

test('the ⚙ view options popover exposes document controls persisted per project', async ({
  page,
}) => {
  const { pid, sheetId } = await seedDocSheet(page);
  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-view')).toBeVisible();

  await page.getByTestId('document-options-button').click();
  const options = page.getByTestId('document-options');
  await expect(options).toBeVisible();
  await expect(page.getByTestId('document-option-layout')).toBeVisible();
  await expect(page.getByTestId('document-option-fit')).toBeVisible();
  await expect(page.getByTestId('document-option-textlayer')).toBeVisible();
  await expect(page.getByTestId('document-option-sync')).toHaveCount(0);
  await expect(page.getByTestId('document-option-title')).toBeVisible();

  // Changing the fit persists across reload (per-project chrome state).
  await page.getByTestId('document-option-fit').selectOption('page');
  await page.reload();
  await page.getByTestId('view-switch-document').click();
  await page.getByTestId('document-options-button').click();
  await expect(page.getByTestId('document-option-fit')).toHaveValue('page');
});

test('switching documents with Row Detail open syncs the Detail to the new row', async ({
  page,
}) => {
  const { pid, sheetId } = await seedDocSheet(page, { extraDocs: true });
  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-document').click();

  // Open the Detail for the first document, then pick the second in the list:
  // the Detail must follow (a stale Detail beside a new document reads as a bug).
  const items = page.getByTestId('document-list-item');
  await items.first().click();
  await page.getByTestId('document-open-detail').click();
  await expect(page.getByTestId('inspect-detail-rownum')).toHaveText('ROW 1');

  await items.nth(1).click();
  await expect(page.getByTestId('inspect-detail-rownum')).toHaveText('ROW 2');
});

test('an audio-bearing sheet is eligible for Document view and the reader renders a playable audio element (frontend-affordance-fixes)', async ({
  page,
}) => {
  // documentMedia.ts's DOCUMENT_MEDIA_COLUMN_TYPES admitted only file/image/
  // video; audio-bearing sheets never got the Document pill even though
  // DocumentReader already had a native <audio controls> rendering branch for
  // it. The "Council audio" seeded project (media.spec.ts's fixture) has a
  // real `audio`-typed `media` column — reuse it rather than re-seeding.
  const pid = await projectIdByName(page.request, 'Council audio');
  const sheets = await listSheets(page.request, pid);
  const sheetId = sheets[0].id;
  const columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.find((c) => c.name === 'media')?.type).toBe('audio');

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible();

  // Eligibility: the Document segment is present and enabled for this
  // audio-only sheet — not just visible-but-disabled.
  const documentSegment = page.getByTestId('view-switch-document');
  await expect(documentSegment).toBeVisible();
  await expect(documentSegment).toBeEnabled();

  // Reachability + rendering: switching in replaces the grid with the reader,
  // the header names the media-type chip "Audio", and a real playable
  // <audio> element with a resolved blob src is present (the same URL shape
  // media.spec.ts's row-drawer assertion pins).
  await documentSegment.click();
  await expect(documentSegment).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('document-view')).toBeVisible();
  await expect(page.getByTestId('grid')).toHaveCount(0);
  await expect(page.getByTestId('document-reader-chip')).toContainText(/audio/i);

  const audio = page.getByTestId('document-audio').locator('audio');
  await expect(audio).toBeVisible();
  const src = await audio.getAttribute('src');
  expect(src).toMatch(new RegExp(`/api/projects/${pid}/blobs/`));
});
