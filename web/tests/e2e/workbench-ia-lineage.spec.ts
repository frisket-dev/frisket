import { test, expect, type APIRequestContext } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import {
  createProject,
  editCells,
  listSheets,
  openProject,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';

// Workbench IA increment 7 — LIVE derived-sheet lineage UI.
//
// Drives REAL staleness end-to-end (no faked syncState): import a parent with a
// JSON list column, derive a MODEL-FREE child from it (derive.table_from_list
// column source), then edit the parent's cell so the backend's lazy resolver
// reports the child stale. The tab sync dot flips green -> amber, the sheet-info
// popover shows Stale + the real transform label, Re-run now refreshes without a
// cost confirm and the dot returns green, the cascade pill counts the stale
// sheet, the Monitor Lineage tab renders the three-tier DAG with the stale node
// amber, and a model-backed sheet's Re-run now pauses at the cost confirm.

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

// A derived sheet whose parent op ran a MODEL: seeded directly into the bundle
// (the same direct-python fixture idiom workbench-ia-sheet-tabs.spec.ts uses to
// prime the derive cache) so the sheet.refresh cost gate can be asserted with
// NO live model calls — the 402 fires before any model would be touched.
function seedModelBackedDerivedSheet(pid: string, parentSheetId: number): number {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import sys
from pathlib import Path
from frisket.engine.store import Project

workspace, pid, parent_id = sys.argv[1], sys.argv[2], int(sys.argv[3])
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    op_id = project.append_op("reduce.group_summary", {"sheet_id": parent_id})
    project.db.execute(
        "INSERT INTO runs (op_id, sheet_id, recipe, status, model, cost_estimate) "
        "VALUES (?, ?, 'reduce', 'completed', 'stub/model', 0.42)",
        (op_id, parent_id),
    )
    sheet_id = project.add_sheet("Summary", parent_sheet_id=parent_id, parent_op_id=op_id)
    project.db.commit()
    print(sheet_id)
finally:
    project.close()
`;
  const out = execFileSync(
    'uv',
    ['run', 'python', '-c', script, workspace, pid, String(parentSheetId)],
    { cwd: REPO_ROOT, stdio: 'pipe', timeout: 60_000 },
  );
  return Number(out.toString().trim());
}

async function postAction(
  request: APIRequestContext,
  pid: string,
  kind: string,
  params: Record<string, unknown>,
  sheetName: string,
): Promise<Record<string, unknown>> {
  const spec = {
    action_id: kind,
    scope: { kind: 'project' },
    sheet_name: sheetName,
    params,
    idempotency_key: `e2e-${kind}-${randomUUID().replaceAll('-', '')}`,
  };
  const res = await request.post(`/api/projects/${pid}/actions/v1/run`, { data: spec });
  const body = (await res.json()) as Record<string, unknown>;
  expect(res.ok(), JSON.stringify(body)).toBeTruthy();
  expect(body.status, JSON.stringify(body)).toBe('completed');
  return body;
}

async function postProjectAction(
  request: APIRequestContext,
  pid: string,
  actionId: string,
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const res = await request.post(`/api/projects/${pid}/actions/v1/run`, {
    data: {
      action_id: actionId,
      scope: { kind: 'project' },
      params,
      output_names: {},
      idempotency_key: `e2e-${actionId}-${randomUUID().replaceAll('-', '')}`,
    },
  });
  const body = (await res.json()) as Record<string, unknown>;
  expect(res.ok(), JSON.stringify(body)).toBeTruthy();
  expect(body.status, JSON.stringify(body)).toBe('completed');
  return body;
}

async function seedDerivedProject(request: APIRequestContext): Promise<{
  pid: string;
  parentId: number;
  childId: number;
  jsonColumnId: number;
  firstRowId: number;
}> {
  const pid = await createProject(request, uniqueName('lineage'));
  await postAction(request, pid, 'import.rows', {
    columns: [
      { name: 'title', type: 'text' },
      { name: 'rows_json', type: 'json' },
    ],
    rows: [
      { title: 'Q1', rows_json: [{ vendor: 'Acme' }, { vendor: 'Globex' }] },
      { title: 'Q2', rows_json: [{ vendor: 'Initech' }] },
    ],
  }, 'Filings');
  const sheets = await listSheets(request, pid);
  const parent = sheets.find((s) => s.name === 'Filings')!;
  const columns = await sheetColumns(request, pid, parent.id);
  const jsonColumnId = columns.find((c) => c.name === 'rows_json')!.id;
  const rows = await sheetData(request, pid, parent.id, 0, 10);
  const firstRowId = Number(rows.rows[0].id);

  await postAction(request, pid, 'derive.table_from_list', {
    source: { kind: 'column', sheet_id: parent.id, column_id: jsonColumnId },
  }, 'Vendors');
  const after = await listSheets(request, pid);
  const child = after.find((s) => s.name === 'Vendors')!;
  return { pid, parentId: parent.id, childId: child.id, jsonColumnId, firstRowId };
}

test('derived tab sync dot flips green->amber on a parent edit; popover shows Stale + real transform', async ({
  page,
  request,
}) => {
  const { pid, childId, jsonColumnId, firstRowId } = await seedDerivedProject(request);
  await openProject(page, pid, childId);

  const dot = page.getByTestId(`workbench-mainView-tab-syncDot-${childId}`);
  await expect(dot).toHaveAttribute('data-sync-state', 'synced');

  // Real parent mutation -> the backend's lazy resolver reports the child stale.
  await editCells(request, pid, [
    { rowId: firstRowId, columnId: jsonColumnId, value: [{ vendor: 'Acme' }, { vendor: 'RENAMED' }] },
  ]);
  await page.reload();
  await expect(
    page.getByTestId(`workbench-mainView-tab-syncDot-${childId}`),
  ).toHaveAttribute('data-sync-state', 'stale');

  // The ⓘ on the active derived tab opens the sheet-info popover.
  await page.getByTestId(`workbench-mainView-tab-info-${childId}`).click();
  const popover = page.getByTestId('sheet-info-popover');
  await expect(popover).toBeVisible();
  await expect(page.getByTestId('sheet-info-badge')).toHaveText('Stale');
  // HOW IT'S MADE carries the REAL op kind/label — never a hardcoded 'derive'.
  await expect(page.getByTestId('sheet-info-transform')).toContainText('derive.table_from_list');
});

test('Re-run now on a model-free derive refreshes without a confirm and the dot returns green', async ({
  page,
  request,
}) => {
  const { pid, childId, jsonColumnId, firstRowId } = await seedDerivedProject(request);
  await editCells(request, pid, [
    { rowId: firstRowId, columnId: jsonColumnId, value: [{ vendor: 'Z' }] },
  ]);
  await openProject(page, pid, childId);
  await expect(
    page.getByTestId(`workbench-mainView-tab-syncDot-${childId}`),
  ).toHaveAttribute('data-sync-state', 'stale');

  await page.getByTestId(`workbench-mainView-tab-info-${childId}`).click();
  const rerun = page.getByTestId('sheet-info-rerun');
  await expect(rerun).toHaveAttribute('type', 'button');
  await expect(rerun).toHaveClass(/\bbtn\b/);
  await expect(rerun).toHaveClass(/\bbtn-primary\b/);
  await rerun.click();

  // Model-free refresh runs immediately (no cost confirm); the dot returns green.
  await expect(
    page.getByTestId(`workbench-mainView-tab-syncDot-${childId}`),
  ).toHaveAttribute('data-sync-state', 'synced');
});

test('the stale cascade pill appears, counts stale sheets, and its confirm lists them', async ({
  page,
  request,
}) => {
  const { pid, childId, jsonColumnId, firstRowId } = await seedDerivedProject(request);
  await openProject(page, pid, childId);
  await expect(page.getByTestId('workbench-mainView-stalePill')).toHaveCount(0);

  await editCells(request, pid, [
    { rowId: firstRowId, columnId: jsonColumnId, value: [{ vendor: 'Q' }] },
  ]);
  await page.reload();

  const pill = page.getByTestId('workbench-mainView-stalePill');
  await expect(pill).toBeVisible();
  await expect(pill).toContainText('re-run 1');

  await pill.click();
  await expect(page.getByTestId('workbench-mainView-cascadeConfirm')).toBeVisible();
  await expect(page.getByTestId(`cascade-stale-${childId}`)).toBeVisible();
});

test('the Monitor Lineage tab renders the three-tier DAG with the stale node amber', async ({
  page,
  request,
}) => {
  const { pid, parentId, childId, jsonColumnId, firstRowId } = await seedDerivedProject(request);
  // Populate the sources tier for real: a source that lands rows on the root.
  await postProjectAction(request, pid, 'source.create', {
    name: 'PACER feed',
    kind: 'url',
    url: 'https://example.test/feed',
    sheet_id: parentId,
  });
  await editCells(request, pid, [
    { rowId: firstRowId, columnId: jsonColumnId, value: [{ vendor: 'DRIFTED' }] },
  ]);
  await openProject(page, pid, childId);

  await page.getByTestId('bottom-dock-tab-lineage').click();
  const panel = page.getByTestId('lineage-panel');
  await expect(panel).toBeVisible();

  // Three tiers: sources → sheets → AI columns.
  await expect(page.getByTestId('lineage-tier-sources')).toBeVisible();
  await expect(page.getByTestId('lineage-tier-sheets')).toBeVisible();
  await expect(page.getByTestId('lineage-tier-ai-columns')).toBeVisible();
  await expect(page.getByTestId('lineage-tier-sources')).toContainText('PACER feed');

  // The stale derived sheet's node + its inbound edge carry the amber marks —
  // read straight from GET /lineage, never computed client-side.
  const staleNode = page.getByTestId(`lineage-node-sheet-${childId}`);
  await expect(staleNode).toHaveAttribute('data-stale', 'true');
  await expect(
    page.getByTestId(`lineage-edge-sheet-${parentId}-sheet-${childId}`),
  ).toHaveAttribute('data-stale', 'true');
  // The root sheet's node is NOT stale-marked (no faked staleness).
  await expect(page.getByTestId(`lineage-node-sheet-${parentId}`)).not.toHaveAttribute(
    'data-stale',
    'true',
  );

  // The 'Re-run N stale · cascades' control shares the SAME confirm as the pill.
  await page.getByTestId('lineage-rerun-stale').click();
  await expect(page.getByTestId('cascade-confirm-run')).toHaveAttribute('type', 'button');
  await expect(page.getByTestId('cascade-confirm-run')).toHaveClass(/\bbtn\b/);
  await expect(page.getByTestId('workbench-mainView-cascadeConfirm')).toBeVisible();
  await expect(page.getByTestId(`cascade-stale-${childId}`)).toBeVisible();
  await page.getByTestId('cascade-confirm-run').click();
  // Confirming enqueues the (model-free) refresh; the DAG re-reads and the
  // node returns to synced.
  await expect(staleNode).not.toHaveAttribute('data-stale', 'true');
});

test("sheet-info's View lineage focuses the Monitor Lineage tab (expanding a collapsed dock)", async ({
  page,
  request,
}) => {
  const { pid, childId } = await seedDerivedProject(request);
  // Start with the dock minimized so the focus must also expand it.
  await page.addInitScript(() => {
    localStorage.setItem('frisket:bottom-dock-collapsed', '1');
  });
  await openProject(page, pid, childId);
  await expect(page.getByTestId('workbench-region-bottomDock')).toHaveAttribute(
    'data-collapsed',
    'true',
  );

  await page.getByTestId(`workbench-mainView-tab-info-${childId}`).click();
  await page.getByTestId('sheet-info-view-lineage').click();

  await expect(page.getByTestId('bottom-dock-tab-lineage')).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await expect(page.getByTestId('lineage-panel')).toBeVisible();
  await expect(page.getByTestId('workbench-region-bottomDock')).not.toHaveAttribute(
    'data-collapsed',
    'true',
  );
});

test('Re-run now on a MODEL-backed derived sheet pauses at the cost confirm (no auto-spend)', async ({
  page,
  request,
}) => {
  const { pid, parentId } = await seedDerivedProject(request);
  const modelChildId = seedModelBackedDerivedSheet(pid, parentId);

  await openProject(page, pid, modelChildId);
  await page.getByTestId(`workbench-mainView-tab-info-${modelChildId}`).click();
  await expect(page.getByTestId('sheet-info-popover')).toBeVisible();

  await page.getByTestId('sheet-info-rerun').click();
  // The server 402s with the estimate; the popover pauses for consent instead
  // of spending — the priced line renders and Re-run now is disabled.
  const gate = page.getByTestId('sheet-info-cost-confirm');
  await expect(gate).toBeVisible();
  await expect(gate).toContainText('$0.42');
  await expect(page.getByTestId('sheet-info-rerun')).toBeDisabled();
});
