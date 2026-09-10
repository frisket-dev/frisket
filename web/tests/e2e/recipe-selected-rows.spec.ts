// RED-FIRST acceptance for recipe-selected-rows-ui:
// selecting grid rows lets an action run only those row_ids, and the visible
// run progress is scoped to that selected count.

import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  sheetData,
  uniqueName,
} from './helpers';

const HEADER_HEIGHT = 34;
const ROW_HEIGHT = 34;
const MARKER_X = 20;

async function clickRowMarker(page: Page, rowIndex: number): Promise<void> {
  const box = await page.getByTestId('grid').boundingBox();
  expect(box).toBeTruthy();
  await page.mouse.click(
    box!.x + MARKER_X,
    box!.y + HEADER_HEIGHT + rowIndex * ROW_HEIGHT + ROW_HEIGHT / 2,
  );
}

test('action run submits explicit row_ids for selected rows and shows row-scoped progress', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-selected-rows'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    [
      'note',
      '"first selected row"',
      '"unselected middle row"',
      '"second selected row"',
      '"unselected tail row"',
    ].join('\n'),
  );
  const data = await sheetData(page.request, pid, sheetId, 0, 10);
  const expectedRowIds = [data.rows[0].id, data.rows[2].id];

  let postedAction: Record<string, unknown> | null = null;
  let sawRun!: () => void;
  const runStarted = new Promise<void>((resolve) => {
    sawRun = resolve;
  });
  let releaseRun!: () => void;
  const runBlocked = new Promise<void>((resolve) => {
    releaseRun = resolve;
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    postedAction = route.request().postDataJSON() as Record<string, unknown>;
    sawRun();
    await runBlocked;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'map.python', action_id: 'act-selected-rows' },
        status: 'completed',
        project_id: pid,
        run_id: 9201,
        receipt_id: 'receipt-selected-rows',
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/9201/status`, async (route) => {
    const publicStatus = {
      run_id: 9201,
      action_kind: 'map.python',
      action_name: 'Python',
      status: 'completed',
      total: 2,
      completed: 2,
      failed: 0,
      cost: 0,
      live: false,
    };
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.actions.v1',
        action: 'run_status',
        project_id: pid,
        run: {
          id: 9201,
          sheet_id: sheetId,
          action_kind: 'map.python',
          action_name: 'Python',
          status: 'completed',
          total_rows: 2,
          completed_rows: 2,
          failed_rows: 0,
          cost_actual: 0,
          cost_estimate: 0,
          public_status: publicStatus,
        },
      }),
    });
  });

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await clickRowMarker(page, 0);
  await clickRowMarker(page, 2);
  if (await page.getByTestId('row-drawer').isVisible()) {
    await page.getByLabel('Close drawer').click();
    await expect(page.getByTestId('row-drawer')).not.toBeVisible();
  }

  await openAction(page, 'map.python');
  await expect(page.getByTestId('generated-action-run')).toBeVisible();
  await page
    .getByTestId('field-code')
    .fill("import time\ntime.sleep(0.2)\nresult = row.get('note', '')");

  await page.getByTestId('generated-action-run-scope-menu-button').click();
  await expect(page.getByTestId('generated-action-row-scope-selected')).toContainText('2 selected');
  await page.getByTestId('generated-action-row-scope-selected').click();
  await expect(page.getByTestId('active-run-banner')).toContainText('0/2 rows', {
    timeout: 5_000,
  });
  await runStarted;
  expect(postedAction?.action_id).toBe('map.python');
  expect((postedAction?.scope as Record<string, unknown> | undefined)?.row_ids).toEqual(
    expectedRowIds,
  );

  releaseRun();
  await expect(page.getByTestId('run-progress')).toContainText('2 rows', {
    timeout: 15_000,
  });
});

test('NER visibly refuses an unrepresentable selected-row scope before any run POST', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-ner-scope-containment'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'people.csv',
    [
      'note',
      'Ada Lovelace wrote the first algorithm',
      'Grace Hopper developed the first compiler',
      'Katherine Johnson calculated orbital paths',
    ].join('\n'),
  );

  // The run is intercepted below; keep this scope-containment test independent
  // of whether the optional local NER runtime is installed on the test host.
  await page.route(`**/api/projects/${pid}/actions/v1/catalog`, async (route) => {
    const response = await route.fetch();
    const body = await response.json() as {
      actions: Array<{
        kind: string;
        ui_hints?: { engines?: Array<Record<string, unknown>> };
      }>;
    };
    const ner = body.actions.find((action) => action.kind === 'map.ner');
    const spacy = ner?.ui_hints?.engines?.find((engine) => engine.id === 'spacy');
    if (spacy) {
      spacy.available = true;
      spacy.error = null;
    }
    await route.fulfill({ response, json: body });
  });

  let postedRunCount = 0;
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    postedRunCount += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'map.ner', action_id: 'act-ner-containment' },
        status: 'completed',
        project_id: pid,
        run_id: 9401,
        receipt_id: 'receipt-ner-containment',
        errors: [],
      }),
    });
  });

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await clickRowMarker(page, 0);
  await clickRowMarker(page, 2);
  if (await page.getByTestId('row-drawer').isVisible()) {
    await page.getByLabel('Close drawer').click();
  }

  await openAction(page, 'map.ner');
  await expect(page.getByTestId('row-scope-summary')).toContainText('2 selected rows');

  const runRequest = page.waitForRequest(
    (request) => request.method() === 'POST'
      && new URL(request.url()).pathname === `/api/projects/${pid}/actions/v1/run`,
    { timeout: 5_000 },
  ).then(() => 'posted' as const, () => 'no-post' as const);
  const visibleRefusal = page.getByRole('alert').filter({
    hasText: /selected(?:-row| rows?).*(?:not supported|cannot|can't|unavailable)|(?:not supported|cannot|can't|unavailable).*selected(?:-row| rows?)/i,
  }).waitFor({ state: 'visible', timeout: 5_000 })
    .then(() => 'refused' as const, () => 'no-refusal' as const);

  await page.getByTestId('run-button').click();
  expect(await Promise.race([visibleRefusal, runRequest])).toBe('refused');
  expect(postedRunCount).toBe(0);
});
