// GenericGraphView — the generic node/edge graph work-view.
//
// Drives the REAL backend end to end (no route mocking): a derive.join output
// is the freshest edge producer, materializing two-sided
// join_left/join_right membership that GET /sheets/:id/graph turns into an
// undirected node/edge graph. The graph segment is
// PRESENT on the join (edge-shaped) sheet and ABSENT on a plain CSV sheet (the
// honesty gate, graph-view-availability-signal-v1); the view renders nodes +
// edges; selecting a node opens the endpoint row drawer and selecting an edge
// opens the edge row drawer; and the config bindings (direction, node label)
// take effect.
// The separate FtM `/graph/neighborhood` service supports entity-detail
// connections; this spec covers only the generic graph view.

import { expect, test, type APIRequestContext } from '@playwright/test';
import { createProject, openProject, uniqueName } from './helpers';

async function importNamedCsv(
  request: APIRequestContext,
  pid: string,
  sheetName: string,
  csv: string,
): Promise<number> {
  const res = await request.post(`/api/projects/${pid}/import/csv?sheet_name=${sheetName}`, {
    multipart: {
      file: { name: `${sheetName}.csv`, mimeType: 'text/csv', buffer: Buffer.from(csv) },
    },
  });
  expect(res.ok()).toBeTruthy();
  return (await res.json()).sheet_id;
}

async function postAction(
  request: APIRequestContext,
  pid: string,
  kind: string,
  params: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const spec = {
    schema_version: 'frisket.action.v2',
    kind,
    capabilities: ['project:write'],
    params,
    idempotency_key: `e2e-${kind}@sha256:${Math.random().toString(16).slice(2)}`,
  };
  const res = await request.post(`/api/projects/${pid}/actions/v1/run`, { data: spec });
  const body = (await res.json()) as Record<string, unknown>;
  expect(res.ok(), JSON.stringify(body)).toBeTruthy();
  expect(body.status, JSON.stringify(body)).toBe('completed');
  return body;
}

/** Seed a derive.join edge sheet + return the plain-CSV left sheet id too. */
async function seedJoinGraph(
  request: APIRequestContext,
  namePrefix: string,
): Promise<{ pid: string; edgeSheetId: number; plainSheetId: number }> {
  const pid = await createProject(request, uniqueName(namePrefix));
  const plainSheetId = await importNamedCsv(
    request,
    pid,
    'States',
    'code,name\nNY,New York\nCA,California\n',
  );
  const rightId = await importNamedCsv(
    request,
    pid,
    'Population',
    'code,pop\nNY,100\nCA,200\n',
  );
  const result = await postAction(request, pid, 'derive.join', {
    left_sheet_id: plainSheetId,
    right_sheet_id: rightId,
    join_keys: [{ left_column: 'code', right_column: 'code' }],
    how: 'inner',
    target_sheet_name: 'States x Population',
  });
  const output = (result.outputs as Array<Record<string, unknown>>).find(
    (o) => o.kind === 'sheet',
  );
  return { pid, edgeSheetId: Number(output!.sheet_id), plainSheetId };
}

test('the graph segment is offered on a join (edge) sheet and hidden on a plain CSV sheet', async ({
  page,
  request,
}) => {
  const { pid, edgeSheetId, plainSheetId } = await seedJoinGraph(request, 'graph-view-gate');

  // Edge-shaped join sheet → segment PRESENT (honest availability).
  await openProject(page, pid, edgeSheetId);
  await expect(page.getByTestId('view-switch-graph')).toBeVisible();

  // Plain CSV import → segment ABSENT (the anti-precedent's offer-everywhere is dead).
  await openProject(page, pid, plainSheetId);
  await expect(page.getByTestId('view-switch-graph')).toHaveCount(0);
});

test('the graph view renders nodes and edges; a node selection opens the endpoint row drawer', async ({
  page,
  request,
}) => {
  const { pid, edgeSheetId } = await seedJoinGraph(request, 'graph-view-render');
  await openProject(page, pid, edgeSheetId);

  await page.getByTestId('view-switch-graph').click();

  await expect(page.getByTestId('graph-view')).toBeVisible();
  await expect(page.getByTestId('graph-svg')).toBeVisible();

  // 2 join rows (NY, CA) → 2 undirected edges; 4 distinct endpoint nodes.
  const nodeRows = page.locator('[data-testid^="graph-node-row-"]');
  const edgeRows = page.locator('[data-testid^="graph-edge-row-"]');
  await expect(nodeRows).toHaveCount(4);
  await expect(edgeRows).toHaveCount(2);
  await expect(page.locator('[data-testid^="graph-node-"]').first()).toBeVisible();

  // Affordance fix (walk #2, finding D): the side-list rows NAVIGATE (open the
  // row drawer) while the SVG dots/edges only SELECT in place -- that split is
  // intentional but was visually undifferentiated. Every row must carry a
  // visible + accessible "this opens something" cue.
  await expect(nodeRows.first().getByTestId('graph-list-open-cue')).toBeVisible();
  await expect(nodeRows.first()).toHaveAttribute('aria-label', /^Open row for /);

  // Select a node → the endpoint row drawer opens (openRowRef navigates to the
  // endpoint row on its own sheet).
  await nodeRows.first().click();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
});

test('an edge selection opens the edge row drawer', async ({ page, request }) => {
  const { pid, edgeSheetId } = await seedJoinGraph(request, 'graph-view-edge-drawer');
  await openProject(page, pid, edgeSheetId);
  await page.getByTestId('view-switch-graph').click();
  await expect(page.getByTestId('graph-svg')).toBeVisible();

  const edgeRows = page.locator('[data-testid^="graph-edge-row-"]');
  await expect(edgeRows).toHaveCount(2);
  await expect(edgeRows.first().getByTestId('graph-list-open-cue')).toBeVisible();
  await expect(edgeRows.first()).toHaveAttribute('aria-label', /^Open row for /);
  await edgeRows.first().click();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
});

test('config bindings take effect: direction toggle and node-label rebinding', async ({
  page,
  request,
}) => {
  const { pid, edgeSheetId } = await seedJoinGraph(request, 'graph-view-config');
  await openProject(page, pid, edgeSheetId);
  await page.getByTestId('view-switch-graph').click();
  await expect(page.getByTestId('graph-svg')).toBeVisible();

  const edgeList = page.getByTestId('graph-edge-list');
  const nodeList = page.getByTestId('graph-node-list');

  // A join is symmetric → undirected by default.
  await expect(edgeList).toContainText('undirected');

  // Direction toggle → edges become directed.
  await page.getByTestId('graph-config-direction').selectOption('directed');
  await expect(edgeList).toContainText('directed');

  // Node-label rebinding → the Population "pop" values surface as node labels.
  await expect(nodeList).not.toContainText('100');
  await page
    .getByTestId('graph-config-node-label')
    .selectOption({ label: 'Population · pop' });
  await expect(nodeList).toContainText('100');
});
