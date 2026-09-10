// RED-FIRST acceptance for derive-preview-progress-ui:
// clicking Preview on a derive recipe shows the same visible active-run
// progress surface while the v1 map.extract preview request is still in flight.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  uniqueName,
} from './helpers';

test('derive preview shows active progress while awaiting the preview response', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-derive-preview-progress'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'stories.csv',
    [
      'story',
      '"Ada met the mayor and the comptroller."',
      '"Ben called the parks commissioner."',
      '"Cara quoted the transit chief."',
      '"Dev interviewed the library director."',
      '"Eli cited the school board chair."',
      '"Fay mentioned the county clerk."',
    ].join('\n'),
  );

  let extractSpec: Record<string, unknown> | null = null;
  let sawExtract!: () => void;
  const extractStarted = new Promise<void>((resolve) => {
    sawExtract = resolve;
  });
  let releaseExtract!: () => void;
  const extractBlocked = new Promise<void>((resolve) => {
    releaseExtract = resolve;
  });

  await page.route(`**/api/projects/${pid}/derive`, async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'derive preview should use v1 actions' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    if (body.kind === 'map.extract') {
      extractSpec = body.params as Record<string, unknown>;
      sawExtract();
      await extractBlocked;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          action: { kind: 'map.extract', action_id: 'act-preview-extract' },
          status: 'completed',
          project_id: pid,
          run_id: 9301,
          receipt_id: 'receipt-preview-extract',
          outputs: [
            {
              kind: 'named_result',
              name: 'people',
              sheet_id: sheetId,
              column_id: 9304,
              row_ids: [1, 2, 3, 4, 5],
              ref: {
                kind: 'named_result',
                sheet_id: sheetId,
                column_id: 9304,
                run_id: 9301,
                op_id: 9303,
                route: 'people',
                schema: 'people_list',
                item_schema: { type: 'string' },
                row_ids: [1, 2, 3, 4, 5],
                may_feed: ['derive.table_from_list'],
              },
            },
          ],
          errors: [],
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'derive.table_from_list', action_id: 'act-preview-derive' },
        status: 'completed',
        project_id: pid,
        run_id: null,
        receipt_id: 'receipt-preview-derive',
        outputs: [],
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/9301/status`, async (route) => {
    const publicStatus = {
      run_id: 9301,
      action_kind: 'map.extract',
      action_name: 'Derive rows preview',
      status: 'completed',
      total: 5,
      completed: 5,
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
          id: 9301,
          sheet_id: sheetId,
          action_kind: 'map.extract',
          action_name: 'Derive rows preview',
          status: 'completed',
          total_rows: 5,
          completed_rows: 5,
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
  await openAction(page, 'derive.table_from_list');
  await expect(page.getByTestId('action-form')).toBeVisible();

  await page.getByTestId('preview-button').click();
  await extractStarted;

  await expect(page.getByTestId('active-run-banner')).toBeVisible();
  await expect(page.getByTestId('active-run-banner')).toContainText('Derive rows preview');
  await expect(page.getByTestId('active-run-banner')).toContainText('0/5 rows');
  expect((extractSpec?.row_ids as unknown[] | undefined)?.length).toBe(5);

  releaseExtract();
  await expect(page.getByTestId('run-progress')).toContainText('5 rows', {
    timeout: 15_000,
  });
});
