// export.column_tables bulk export. Sibling to
// pdf-tables-picker.spec.ts (which stays green): two entry points to the SAME
// export.column_tables action -- the picker's "Export all tables…" button
// (beside Materialize, opened from the pdf_tables post-run completion state)
// and a JSON column's caret menu item ("Export tables…"), which opens the
// same picker decoupled from any open drawer. Both post export.column_tables
// with group_by=table_index + the 8 pdf_tables metadata keys excluded, and
// the resulting zip + manifest.json are verified directly against the
// project's blob store (the no-config default destination is project_file --
// a content-addressed blob write that always succeeds, unlike local_dir which
// would need a directory the browser cannot promise exists).

import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { stubPdfTableProducer } from './pdfTableFixtures';
import {
  clickHeaderMenu,
  createProject,
  editCells,
  importCsv,
  openAction,
  openPalette,
  openProject,
  revealRibbonAction,
  sheetColumns,
  sheetData,
  selectRow,
  uniqueName,
} from './helpers';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

const PDF_TABLE_EXCLUDED_COLUMNS = [
  'source_row_id',
  'source_filename',
  'source_blob_hash',
  'page_start',
  'page_end',
  'table_index',
  'table_row_index',
  'raw_cells_json',
];

function expectColumnTablesPost(
  body: unknown,
  ids: { sheetId: number; columnId: number },
): void {
  expect(body).toEqual({
    action_id: 'export.column_tables',
    scope: { kind: 'project' },
    params: {
      sheet_id: ids.sheetId,
      column_id: ids.columnId,
      destination: { kind: 'project_file', prefix: 'exports/tables' },
      group_by: 'table_index',
      exclude_columns: PDF_TABLE_EXCLUDED_COLUMNS,
    },
    output_names: {},
    idempotency_key: expect.stringMatching(
      /^web-export\.column_tables:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    ),
  });
}

// Copied from pdf-tables-picker.spec.ts (same fixture convention: a bare JSON
// column seeded directly rather than running the real natural_pdf pipeline).
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

interface ZipInspection {
  names: string[];
  manifest: {
    kind: string;
    shape: string;
    group_by: string | null;
    artifact_count: number;
    entry_count: number;
    artifacts: Array<{ filename: string; group_value: unknown; entry_count: number; columns: string[] }>;
  };
}

/** Read the export.column_tables receipt back from the project's sqlite store,
 *  resolve its project_file blob, and unzip it in-process -- the same
 *  backend-state-assertion idiom addJsonColumn uses above. */
function inspectExportZip(pid: string, receiptId: string): ZipInspection {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import io
import json
import sys
import zipfile
from pathlib import Path
from frisket.engine.store import Project

workspace, pid, receipt_id = sys.argv[1], sys.argv[2], sys.argv[3]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    row = project.db.execute("SELECT body FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if row is None:
        raise SystemExit(f"no receipt {receipt_id}")
    receipt = json.loads(row["body"])
    ref = receipt["exports"][0]
    if ref.get("destination_kind") == "project_file":
        content = project.read_blob(ref["blob_hash"])
    else:
        content = Path(ref["path"]).read_bytes()
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = sorted(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
    print(json.dumps({"names": names, "manifest": manifest}))
finally:
    project.close()
`;
  const out = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, receiptId], {
    cwd: REPO_ROOT,
    stdio: 'pipe',
    timeout: 60_000,
  });
  return JSON.parse(out.toString().trim()) as ZipInspection;
}

async function seedPdfTablesColumn(page: import('@playwright/test').Page, pid: string): Promise<{
  sheetId: number;
  columnId: number;
}> {
  const sheetId = await importCsv(page.request, pid, 'sources.csv', 'title\n"Filing A"\n');
  const columnId = addJsonColumn(pid, sheetId, 'pdf_tables');
  await stubPdfTableProducer(page, pid, columnId);
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
  return { sheetId, columnId };
}

test('picker "Export all tables…" runs export.column_tables and writes a zip + manifest.json', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-export-column-tables'));
  const { sheetId, columnId } = await seedPdfTablesColumn(page, pid);

  await openProject(page, pid, sheetId);
  await selectRow(page, 0);
  await openAction(page, 'media.extract_pdf_tables');
  await page.getByTestId('field-output-pdf_tables').fill('pdf_tables');
  await page.getByTestId('review-tables').click();
  const picker = page.getByTestId('pdf-tables-picker');
  await expect(picker).toBeVisible();

  const exportButton = picker.getByTestId('pdf-table-export-tables');
  await expect(exportButton).toBeEnabled();

  const [response] = await Promise.all([
    page.waitForResponse(
      (res) => res.url().includes(`/api/projects/${pid}/actions/v1/run`) && res.request().method() === 'POST',
    ),
    exportButton.click(),
  ]);
  expect(response.ok()).toBeTruthy();
  expectColumnTablesPost(response.request().postDataJSON(), { sheetId, columnId });
  const body = (await response.json()) as {
    action: { kind: string };
    status: string;
    receipt_id: string | null;
    outputs?: Array<{ kind: string; ref: Record<string, unknown> }>;
  };
  expect(body.action.kind).toBe('export.column_tables');
  expect(body.status).toBe('completed');
  const receiptId = body.receipt_id as string;
  expect(receiptId).toBeTruthy();

  const outputs = body.outputs ?? [];
  const exportOutput = outputs.find((o) => o.kind === 'export');
  expect(exportOutput).toBeTruthy();
  expect(exportOutput?.ref.format).toBe('zip');
  expect(exportOutput?.ref.shape).toBe('dict');
  expect(exportOutput?.ref.group_by).toBe('table_index');
  expect(exportOutput?.ref.artifact_count).toBe(2);

  const inspected = inspectExportZip(pid, receiptId);
  expect(inspected.names).toEqual(['000_000.csv', '000_001.csv', 'manifest.json']);
  expect(inspected.manifest.shape).toBe('dict');
  expect(inspected.manifest.group_by).toBe('table_index');
  expect(inspected.manifest.artifact_count).toBe(2);
  expect(inspected.manifest.entry_count).toBe(3);
  const byFilename = Object.fromEntries(
    inspected.manifest.artifacts.map((a) => [a.filename, a]),
  );
  expect(byFilename['000_000.csv'].entry_count).toBe(2);
  expect(byFilename['000_000.csv'].columns).toEqual(['vendor', 'amount']);
  expect(byFilename['000_000.csv'].group_value).toBe(0);
  expect(byFilename['000_001.csv'].entry_count).toBe(1);
  expect(byFilename['000_001.csv'].group_value).toBe(1);
  // The 8 pdf_tables metadata keys (+ the group_by key itself) never leak.
  for (const artifact of inspected.manifest.artifacts) {
    expect(artifact.columns).not.toContain('table_index');
    expect(artifact.columns).not.toContain('source_row_id');
    expect(artifact.columns).not.toContain('raw_cells_json');
  }
});

test('JSON column caret menu offers "Export tables…" and opens the picker', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-export-column-tables-caret'));
  const { sheetId, columnId } = await seedPdfTablesColumn(page, pid);

  await openProject(page, pid, sheetId);
  const columns = await sheetColumns(page.request, pid, sheetId);

  // A text column's caret menu does NOT offer the export item.
  await clickHeaderMenu(page, columns, 'title');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
  await expect(page.getByTestId('header-menu-export-column-tables')).toHaveCount(0);
  await page.keyboard.press('Escape');

  await clickHeaderMenu(page, columns, 'pdf_tables');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
  const exportItem = page.getByTestId('header-menu-export-column-tables');
  await expect(exportItem).toBeVisible();

  const [response] = await Promise.all([
    page.waitForResponse(
      (res) => res.url().includes(`/api/projects/${pid}/actions/v1/run`) && res.request().method() === 'POST',
    ),
    (async () => {
      await exportItem.click();
      const picker = page.getByTestId('pdf-tables-picker');
      await expect(picker).toBeVisible();
      await expect(picker.getByTestId('pdf-table-group-0')).toBeVisible();
      await expect(picker.getByTestId('pdf-table-group-1')).toBeVisible();
      await picker.getByTestId('pdf-table-export-tables').click();
    })(),
  ]);
  expect(response.ok()).toBeTruthy();
  expectColumnTablesPost(response.request().postDataJSON(), { sheetId, columnId });
  const body = (await response.json()) as { action: { kind: string }; status: string };
  expect(body.action.kind).toBe('export.column_tables');
  expect(body.status).toBe('completed');
});

test('Act Export owns column-table export without the generic ActionForm or Misc fallback', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-export-column-tables-act'));
  const { sheetId, columnId } = await seedPdfTablesColumn(page, pid);

  await openProject(page, pid, sheetId);

  await expect(page.getByTestId('ribbon-action-export.column_tables')).toHaveCount(0);
  if (await page.getByTestId('ribbon-tab-misc').count()) {
    await page.getByTestId('ribbon-tab-misc').click();
    await expect(page.getByTestId('ribbon-action-export.column_tables')).toHaveCount(0);
  }

  const palette = await openPalette(page);
  await expect(palette.getByTestId('palette-action-item').first()).toBeVisible();
  await palette.getByTestId('command-palette-input').fill('Export column tables');
  await expect(
    palette.locator('[data-testid="palette-action-item"][data-action-kind="export.column_tables"]'),
  ).toHaveCount(0);
  await page.getByLabel('Close command palette').click();

  await page.getByTestId('ribbon-tab-data').click();
  await page.getByTestId('ribbon-command-export-column-tables').click();
  await expect(page.getByTestId('export-column-tables-modal')).toBeVisible();
  await expect(page.getByTestId('action-panel')).toHaveCount(0);
  await page.getByTestId('export-column-tables-cancel').click();

  await page.getByTestId('ribbon-collapse').click();
  await page.getByTestId('menubar-menu-data').click();
  await page.getByTestId('menu-command-export-column-tables').click();
  const modal = page.getByTestId('export-column-tables-modal');
  await expect(modal).toBeVisible();
  await expect(page.getByTestId('action-panel')).toHaveCount(0);

  await modal.getByTestId('export-column-tables-column').selectOption({ label: 'pdf_tables' });
  await modal.getByTestId('export-column-tables-group-by').fill('table_index');
  await modal
    .getByTestId('export-column-tables-exclude-columns')
    .fill('source_row_id\nsource_filename\nsource_blob_hash\npage_start\npage_end\ntable_index\ntable_row_index\nraw_cells_json');

  const [response] = await Promise.all([
    page.waitForResponse(
      (res) => res.url().includes(`/api/projects/${pid}/actions/v1/run`) && res.request().method() === 'POST',
    ),
    modal.getByTestId('export-column-tables-submit').click(),
  ]);
  expect(response.ok()).toBeTruthy();
  expectColumnTablesPost(response.request().postDataJSON(), { sheetId, columnId });
  const body = (await response.json()) as {
    action: { kind: string };
    status: string;
    receipt_id: string | null;
    outputs?: Array<{ kind: string; ref: Record<string, unknown> }>;
  };
  expect(body.action.kind).toBe('export.column_tables');
  expect(body.status).toBe('completed');
  expect(body.receipt_id).toBeTruthy();
  const exportOutput = (body.outputs ?? []).find((output) => output.kind === 'export');
  expect(exportOutput?.ref.format).toBe('zip');
  expect(exportOutput?.ref.group_by).toBe('table_index');

  const download = page.waitForEvent('download');
  await page.getByTestId('export-column-tables-download').click();
  const artifact = await download;
  expect(artifact.suggestedFilename()).toMatch(/\.zip$/);
  const artifactPath = await artifact.path();
  expect(artifactPath).toBeTruthy();
  expect((await fs.readFile(artifactPath!)).subarray(0, 2).toString()).toBe('PK');

  const inspected = inspectExportZip(pid, body.receipt_id as string);
  expect(inspected.names).toEqual(['000_000.csv', '000_001.csv', 'manifest.json']);
  expect(inspected.manifest.group_by).toBe('table_index');
  expect(inspected.manifest.artifact_count).toBe(2);
  for (const artifactEntry of inspected.manifest.artifacts) {
    expect(artifactEntry.columns).not.toContain('source_row_id');
    expect(artifactEntry.columns).not.toContain('table_index');
    expect(artifactEntry.columns).not.toContain('raw_cells_json');
  }
});

test('all three export surfaces fail closed without disabling the PDF drawer or refetching', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-export-column-tables-missing-catalog'));
  const { sheetId } = await seedPdfTablesColumn(page, pid);
  // The fixture seeds through the action API above; count only launcher traffic below.
  let catalogRequests = 0;
  let launcherActionPosts = 0;
  page.on('request', (request) => {
    if (
      request.method() === 'POST'
      && request.url().includes(`/api/projects/${pid}/actions/v1/run`)
    ) launcherActionPosts += 1;
  });
  await page.route('**/actions/v1/catalog', async (route) => {
    catalogRequests += 1;
    const response = await route.fetch();
    const catalog = await response.json() as { actions: Array<{ kind: string }> };
    await route.fulfill({
      response,
      json: {
        ...catalog,
        actions: catalog.actions.filter((entry) => entry.kind !== 'export.column_tables'),
      },
    });
  });

  await openProject(page, pid, sheetId);
  await expect.poll(() => catalogRequests).toBe(1);

  await page.getByTestId('ribbon-tab-data').click();
  await page.getByTestId('ribbon-command-export-column-tables').click();
  const modal = page.getByTestId('export-column-tables-modal');
  await expect(modal.getByTestId('export-column-tables-catalog-unavailable')).toBeVisible();
  await expect(modal.getByTestId('export-column-tables-submit')).toBeDisabled();
  await modal.getByTestId('export-column-tables-cancel').click();

  const columns = await sheetColumns(page.request, pid, sheetId);
  await clickHeaderMenu(page, columns, 'pdf_tables');
  await page.getByTestId('header-menu-export-column-tables').click();
  const picker = page.getByTestId('pdf-tables-picker');
  await expect(picker.getByTestId('pdf-table-materialize')).toBeEnabled();
  await expect(picker.getByTestId('pdf-table-export-tables')).toHaveCount(0);
  await picker.getByTestId('pdf-table-picker-close').click();

  await selectRow(page, 0);
  await (await revealRibbonAction(page, 'media.extract_pdf_tables')).click();
  const drawer = page.getByTestId('action-panel');
  await drawer.getByTestId('field-output-pdf_tables').fill('pdf_tables');
  await expect(drawer.getByRole('alert')).toHaveCount(0);
  await drawer.getByTestId('review-tables').click();
  await expect(picker.getByTestId('pdf-table-materialize')).toBeEnabled();
  await expect(picker.getByTestId('pdf-table-export-tables')).toHaveCount(0);

  expect(catalogRequests).toBe(1);
  expect(launcherActionPosts).toBe(0);
});
