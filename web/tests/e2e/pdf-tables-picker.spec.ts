// PDF-tables picker. After
// media.extract_pdf_tables completes it leaves a JSON output column whose cells
// flatten EVERY extracted table into one array keyed by table_index, with 8
// metadata keys inserted BEFORE the real table columns. The grid renders that as
// raw JSON and RowDrawer's 6-key mini-table shows metadata-only. This picker,
// reachable from the pdf_tables action's post-run completion state, renders a
// structured per-table preview (grouped by table_index, metadata collapsed),
// then materializes a clean child sheet via the ordinary derive.table_from_list
// column source with a metadata-excluding include_columns projection — no
// bespoke materializer, no model call.

import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { stubPdfTableProducer } from './pdfTableFixtures';
import {
  createProject,
  editCells,
  importCsv,
  listSheets,
  openAction,
  openProject,
  selectRow,
  sheetData,
  type WireSheet,
  uniqueName,
} from './helpers';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

// Copied from derive-object-list.spec.ts (P5 owns helper consolidation; this
// mirrors the extract_pdf_tables end-state: a materialized JSON list column).
function addJsonColumn(pid: string, sheetId: number, name: string): number {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import sys
from pathlib import Path
from frisket.engine.store import Project

workspace, pid, sheet_id_raw, name = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    column_id = project.add_column(int(sheet_id_raw), name, "json")
    project.db.commit()
    print(column_id)
finally:
    project.close()
`;
  const out = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, String(sheetId), name], {
    cwd: REPO_ROOT,
    stdio: 'pipe',
    timeout: 60_000,
  });
  return Number(out.toString().trim());
}

async function waitForSheet(
  request: Parameters<typeof listSheets>[0],
  pid: string,
  name: string,
): Promise<WireSheet> {
  for (let i = 0; i < 60; i += 1) {
    const sheet = (await listSheets(request, pid)).find((s) => s.name === name);
    if (sheet) return sheet;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`sheet ${name} did not appear`);
}

// One flattened pdf_tables row: 8 metadata keys FIRST (as the backend emits
// them), then the real table columns. tableIndex/tableRowIndex drive grouping.
function pdfRow(
  tableIndex: number,
  tableRowIndex: number,
  real: Record<string, string>,
): Record<string, unknown> {
  return {
    source_row_id: 1,
    source_filename: 'contracts-2024.pdf',
    source_blob_hash: 'sha256:abc',
    page_start: 1,
    page_end: 1,
    table_index: tableIndex,
    table_row_index: tableRowIndex,
    raw_cells_json: JSON.stringify(Object.values(real)),
    ...real,
  };
}

test('picker previews per-table groups and materializes a metadata-free child sheet via derive.table_from_list', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-pdf-tables-picker'));
  const sheetId = await importCsv(page.request, pid, 'sources.csv', 'title\n"Filing A"\n');
  const columnId = addJsonColumn(pid, sheetId, 'pdf_tables');

  // Two table_index groups with the SAME real shape (matches the extractor's
  // require_matching contract): table 0 has two rows, table 1 has one.
  const before = await sheetData(page.request, pid, sheetId);
  const [rowA] = before.rows.map((r) => r.id);
  await editCells(page.request, pid, [
    {
      rowId: rowA,
      columnId,
      value: [
        pdfRow(0, 0, { vendor: 'Acme', amount: '1200' }),
        pdfRow(0, 1, { vendor: 'Globex', amount: '800' }),
        pdfRow(1, 0, { vendor: 'Initech', amount: '450' }),
      ],
    },
  ]);

  const v1Posts: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    v1Posts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.continue();
  });

  await openProject(page, pid, sheetId);
  await selectRow(page, 0);
  await stubPdfTableProducer(page, pid, columnId);
  await openAction(page, 'media.extract_pdf_tables');
  await page.getByTestId('field-output-pdf_tables').fill('pdf_tables');

  // The post-run completion affordance.
  await page.getByTestId('review-tables').click();
  const picker = page.getByTestId('pdf-tables-picker');
  await expect(picker).toBeVisible();

  // One grouped preview per table_index, REAL columns visible (not raw JSON,
  // not metadata-only).
  const group0 = picker.getByTestId('pdf-table-group-0');
  const group1 = picker.getByTestId('pdf-table-group-1');
  await expect(group0).toBeVisible();
  await expect(group1).toBeVisible();
  await expect(group0.getByTestId('pdf-table-preview')).toContainText('vendor');
  await expect(group0.getByTestId('pdf-table-preview')).toContainText('amount');
  await expect(group0.getByTestId('pdf-table-preview')).toContainText('Acme');
  await expect(group1.getByTestId('pdf-table-preview')).toContainText('Initech');

  // Metadata collapsed by default: the metadata column names are NOT shown in
  // the preview head until the toggle is flipped.
  await expect(group0.getByTestId('pdf-table-preview')).not.toContainText('table_index');
  await expect(group0.getByTestId('pdf-table-preview')).not.toContainText('source_blob_hash');
  await picker.getByTestId('pdf-table-metadata-toggle').click();
  await expect(group0.getByTestId('pdf-table-preview')).toContainText('table_index');
  await picker.getByTestId('pdf-table-metadata-toggle').click();

  await picker.getByTestId('pdf-table-target-name').fill('Bid tables');
  await picker.getByTestId('pdf-table-materialize').click();

  // The posted action IS a derive.table_from_list sourced from the extraction
  // column, projecting ONLY the real columns (metadata excluded).
  await expect.poll(() => v1Posts.length, { timeout: 10_000 }).toBe(1);
  const posted = v1Posts[0];
  expect(posted.action_id).toBe('derive.table_from_list');
  expect(posted.scope).toEqual({ kind: 'project' });
  expect(posted).not.toHaveProperty('schema_version');
  const params = posted.params as Record<string, unknown>;
  const source = params.source as Record<string, unknown>;
  expect(source.kind).toBe('column');
  expect(source.sheet_id).toBe(sheetId);
  expect(source.column_id).toBe(columnId);
  expect(source.include_columns).toEqual(['vendor', 'amount']);
  expect(posted.sheet_name).toBe('Bid tables');
  expect(params).not.toHaveProperty('target_sheet_name');

  // Child sheet: real columns only, metadata excluded, parent lineage intact.
  const child = await waitForSheet(page.request, pid, 'Bid tables');
  await page.goto(`/p/${pid}/s/${child.id}`);
  await expect(page.getByTestId('sheet-breadcrumb')).toContainText('via derive');
  await expect(page.getByTestId('sheet-stats')).toHaveText('3 rows · 2 columns');

  const childData = await sheetData(page.request, pid, child.id);
  expect(childData.columns.map((c) => c.name)).toEqual(['vendor', 'amount']);
  expect(childData.rows.map((r) => r.cells[String(childData.columns[0].id)])).toEqual([
    'Acme',
    'Globex',
    'Initech',
  ]);
});

test('picker fails loudly when table groups have mismatched real shapes', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-pdf-tables-mismatch'));
  const sheetId = await importCsv(page.request, pid, 'sources.csv', 'title\n"Filing A"\n');
  const columnId = addJsonColumn(pid, sheetId, 'pdf_tables');

  const before = await sheetData(page.request, pid, sheetId);
  const [rowA] = before.rows.map((r) => r.id);
  // Divergent real columns across table_index groups.
  await editCells(page.request, pid, [
    {
      rowId: rowA,
      columnId,
      value: [
        pdfRow(0, 0, { vendor: 'Acme', amount: '1200' }),
        pdfRow(1, 0, { region: 'West', total: '50' }),
      ],
    },
  ]);

  const v1Posts: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    v1Posts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.continue();
  });

  await openProject(page, pid, sheetId);
  await selectRow(page, 0);
  await stubPdfTableProducer(page, pid, columnId);
  await openAction(page, 'media.extract_pdf_tables');
  await page.getByTestId('field-output-pdf_tables').fill('pdf_tables');
  await page.getByTestId('review-tables').click();
  const picker = page.getByTestId('pdf-tables-picker');
  await expect(picker).toBeVisible();

  await expect(picker.getByTestId('pdf-table-shape-error')).toBeVisible();
  await expect(picker.getByTestId('pdf-table-materialize')).toBeDisabled();
  // Nothing was posted.
  expect(v1Posts).toHaveLength(0);
});
