import { expect, test } from '@playwright/test';
import {
  clickHeader,
  createProject,
  importCsv,
  openProject,
  runAndWait,
  sheetColumns,
  uniqueName,
} from './helpers';

type WireColumnRunsResponse = {
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
  next_offset: number | null;
  current_run: WireColumnRun | null;
  current_run_loaded: boolean;
  runs: WireColumnRun[];
  column: {
    id: number;
    name: string;
    current_run_id: number | null;
  };
};

type WireColumnRun = {
  run_id: number;
  action_kind: string;
  action_name: string;
  model: string | null;
  status: string;
  spec: Record<string, unknown>;
  total_rows: number;
  completed_rows: number;
  failed_rows: number;
  cost_actual: number;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  current: boolean;
};

function wireColumnRun(runId: number): WireColumnRun {
  return {
    run_id: runId,
    action_kind: 'map.template',
    action_name: 'Template',
    model: null,
    status: 'completed',
    spec: { context: `column history sentinel ${runId}` },
    total_rows: 2,
    completed_rows: 2,
    failed_rows: 0,
    cost_actual: 0,
    started_at: new Date(Date.UTC(2026, 0, 1, 0, 0, runId - 9000)).toISOString(),
    finished_at: new Date(Date.UTC(2026, 0, 1, 0, 1, runId - 9000)).toISOString(),
    duration_ms: 1200,
    tokens_in: null,
    tokens_out: null,
    current: runId === 9001,
  };
}

function columnRunsPage(offset: number, limit: number): WireColumnRunsResponse {
  const total = 25;
  const runIds = Array.from({ length: total }, (_, index) => 9000 + total - index);
  const pageIds = runIds.slice(offset, offset + limit);
  return {
    offset,
    limit,
    total,
    has_more: offset + pageIds.length < total,
    next_offset: offset + pageIds.length < total ? offset + pageIds.length : null,
    current_run: wireColumnRun(9001),
    current_run_loaded: pageIds.includes(9001),
    column: {
      id: 0,
      name: 'topic',
      current_run_id: 9001,
    },
    runs: pageIds.map(wireColumnRun),
  };
}

test('column drawer navigates paged version history', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-column-run-history'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'note\n"alpha"\n"beta"\n',
  );
  await runAndWait(page.request, pid, {
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { template: { text: '{{note}}' } },
    output_names: { rendered: 'topic' },
    idempotency_key: `e2e-column-run-history-map.template:${pid}:${sheetId}`,
  });

  const columns = await sheetColumns(page.request, pid, sheetId);
  const topic = columns.find((column) => column.name === 'topic');
  expect(topic).toBeTruthy();
  expect(topic!.ai_generated).toBe(true);

  const requestedOffsets: string[] = [];
  await page.route(`**/api/projects/${pid}/columns/${topic!.id}/runs?*`, async (route) => {
    const url = new URL(route.request().url());
    const offset = Number(url.searchParams.get('offset') ?? 0);
    const limit = Number(url.searchParams.get('limit') ?? 20);
    requestedOffsets.push(`${offset}:${limit}`);
    const body = columnRunsPage(offset, limit);
    body.column.id = Number(topic!.id);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });

  await openProject(page, pid, sheetId);
  await clickHeader(page, columns, 'topic');

  const drawer = page.getByTestId('column-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId('column-versions').locator('li')).toHaveCount(20);
  await expect(drawer.getByTestId('column-prompt')).toContainText('column history sentinel 9001');
  await expect(drawer.getByTestId('column-versions-page-note')).toContainText('Showing 1-20 of 25');

  requestedOffsets.length = 0;
  await drawer.getByTestId('column-versions-load-older').click();
  await expect(drawer.getByTestId('column-versions').locator('li')).toHaveCount(5);
  await expect(drawer.getByTestId('column-versions-page-note')).toContainText('Showing 21-25 of 25');
  await expect(drawer.getByTestId('column-versions-load-newer')).toBeVisible();
  expect(requestedOffsets).toContain('20:20');
  await expect(drawer).toContainText('v5');
  await expect(drawer).toContainText('current');

  requestedOffsets.length = 0;
  await drawer.getByTestId('column-versions-load-newer').click();
  await expect(drawer.getByTestId('column-versions').locator('li')).toHaveCount(20);
  expect(requestedOffsets).toContain('0:20');
  await expect(drawer.getByTestId('column-versions-page-note')).toContainText('Showing 1-20 of 25');
});
