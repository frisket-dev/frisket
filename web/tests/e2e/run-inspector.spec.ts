// Run inspector: the status bar watcher uses the run-scoped rows endpoint, not
// the current sheet page, and renders failed-row error + retry text.

import { expect, test } from '@playwright/test';
import {
  createProject,
  finishLocalQueuedRun,
  importCsv,
  openAction,
  selectActionInputColumns,
  uniqueName,
} from './helpers';

test('run watcher shows failed row text and retry details', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-run-inspector'));
  const slowText = `${'a'.repeat(5000)}!`;
  const sheetId = await importCsv(page.request, pid, 'rows.csv', `note\n"${slowText}"\n"beta"\n`);

  await page.goto(`/p/${pid}`);
  await openAction(page, 'map.regex_extract');
  await selectActionInputColumns(page, 'note');
  await page.getByTestId('field-pattern').fill('(a+)+$');
  let runActionPayload: Record<string, unknown> | null = null;
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    runActionPayload = {
      ...body,
      params: {
        ...(body.params as Record<string, unknown>),
        timeout_seconds: 0.001,
      },
    };
    await route.continue({ postData: JSON.stringify(runActionPayload) });
  });
  const [runResponse] = await Promise.all([
    page.waitForResponse((resp) =>
      resp.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      resp.request().method() === 'POST',
    ),
    page.getByTestId('generated-action-run').click(),
  ]);
  expect(runResponse.ok()).toBeTruthy();
  expect(runActionPayload).toMatchObject({
    action_id: 'map.regex_extract',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: {
      pattern: '(a+)+$',
      timeout_seconds: 0.001,
    },
    output_names: { extracted: 'extracted' },
    idempotency_key: expect.any(String),
  });
  expect(runActionPayload).not.toHaveProperty('schema_version');
  expect(runActionPayload).not.toHaveProperty('kind');
  expect(runActionPayload).not.toHaveProperty('capabilities');
  const { run_id: runId } = await runResponse.json();

  await page.route(`**/api/projects/${pid}/runs/${runId}/rows*`, async () => {
    throw new Error('run inspector must use the canonical action rows endpoint');
  });
  await page.route(`**/api/projects/${pid}/actions/runs/${runId}/rows*`, async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('status') !== 'error') {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    const body = await response.json();
    if (body.rows?.[0]) {
      body.rows[0].retry_count = 1;
      body.rows[0].retries = [{ status: 429, error: 'rate limited', retryable: true }];
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
  finishLocalQueuedRun(pid, runId);

  await expect(page.getByTestId('run-progress')).toContainText('failed', {
    timeout: 30_000,
  });
  if (!(await page.getByTestId('run-watcher').isVisible())) {
    await page.getByTestId('run-watcher-toggle').click();
  }
  await expect(page.getByTestId('run-inspector-rows')).toBeVisible();
  const row = page.getByTestId('run-inspector-row').first();
  await expect(row).toContainText('regex timed out');
  await expect(row).toContainText('1 retry');
  await expect(row).toContainText('rate limited');
  await expect(row).toContainText('429');
});
