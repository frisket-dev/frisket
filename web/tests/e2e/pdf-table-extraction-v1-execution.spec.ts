import { expect, test } from '@playwright/test';
import {
  createProject,
  dblclickCell,
  importCsv,
  openAction,
  openProject,
  selectRow,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';
import {
  closeActionFormIfOpen,
  seedSourceCellEvidence,
  stubV1ActionRun,
} from './investigativeActionFixtures';

test('PDF table extraction queues media.extract_pdf_tables for selected file rows', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-pdf-tables'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'pdf-table-sources.csv',
    [
      'document,case_id',
      '"contracts-2024.pdf","C-100"',
      '"contracts-2025.pdf","C-101"',
    ].join('\n'),
  );
  let columns = await sheetColumns(page.request, pid, sheetId);
  const documentColumn = columns.find((column) => column.name === 'document');
  expect(documentColumn).toBeTruthy();
  await setColumnType(page.request, pid, documentColumn!.id, 'file');
  seedSourceCellEvidence({
    pid,
    sheetId,
    columnName: 'document',
    producerKind: 'media.extract_pdf_tables',
  });
  columns = await sheetColumns(page.request, pid, sheetId);
  const posts = await stubV1ActionRun({ page, pid, runId: 9821 });

  await openProject(page, pid, sheetId);
  await selectRow(page, 0);
  await openAction(page, 'media.extract_pdf_tables');
  // Exactly ONE
  // column field renders — the generic media-source picker (backend
  // source_requirements, label "PDF column"), not a second bespoke
  // `field-input_column`.
  await expect(page.getByTestId('field-input_column')).toHaveCount(0);
  await expect(page.getByTestId('field-source')).toHaveValue('document');
  await page.getByTestId('field-output-pdf_tables').fill('bid_tables');
  // A10: `shape_policy` (dead, single fixed choice) is gone; `table_mode`
  // (auto|stream|lattice) replaces it.
  await page.getByTestId('field-table_mode').selectOption('lattice');
  await page.getByTestId('generated-action-run').click();

  await expect.poll(() => posts.v1Posts.length, { timeout: 5000 }).toBe(1);
  expect(posts.legacyPosts).toHaveLength(0);
  const posted = posts.v1Posts[0];
  expect(posted.action_id).toBe('media.extract_pdf_tables');
  expect(posted.scope).toEqual({ kind: 'sheet_rows', sheet_id: sheetId, row_ids: [expect.any(Number)] });
  expect(posted.output_names).toEqual({ pdf_tables: 'bid_tables' });
  expect(posted).not.toHaveProperty('capabilities');
  const params = posted.params as Record<string, unknown>;
  expect(params).toMatchObject({
    source: 'document',
    mode: 'extract_table',
    table_mode: 'lattice',
    extract_table: {},
  });
  expect(params).not.toHaveProperty('target_sheet_name');
  expect(params).not.toHaveProperty('source_columns');

  await closeActionFormIfOpen(page);
  await dblclickCell(page, columns, 'document', 0);
  const rowDrawer = page.getByTestId('row-drawer');
  await expect(rowDrawer).toBeVisible();
  await expect(rowDrawer.getByTestId('cell-evidence-active-document')).toBeVisible();
  await rowDrawer.getByTestId('cell-evidence-open-document').click();
  await expect(page.getByTestId('evidence-viewer')).toBeVisible();
});
