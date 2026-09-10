// RED-FIRST (authored 2026-06-13): active runs need visible workspace-level
// feedback and pending AI-cell animation, not just a quiet footer status.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  uniqueName,
} from './helpers';

async function startSimulatedPythonRun(page: import('@playwright/test').Page, pid: string): Promise<number> {
  const runId = 9301;
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'map.python', action_id: 'act-progress' },
        status: 'completed',
        project_id: pid,
        run_id: runId,
        receipt_id: 'receipt-progress',
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/${runId}/status`, async (route) => {
    const publicStatus = {
      run_id: runId,
      action_kind: 'map.python',
      action_name: 'Python',
      status: 'running',
      total: 24,
      completed: 3,
      failed: 0,
      cost: 0,
      live: true,
    };
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.actions.v1',
        action: 'run_status',
        project_id: pid,
        run: {
          id: runId,
          sheet_id: 1,
          action_kind: 'map.python',
          action_name: 'Python',
          status: 'running',
          total_rows: 24,
          completed_rows: 3,
          failed_rows: 0,
          cost_actual: 0,
          cost_estimate: 0,
          public_status: publicStatus,
        },
      }),
    });
  });
  await openAction(page, 'map.python');
  await expect(page.getByTestId('field-code')).toBeVisible();
  await page
    .getByTestId('field-code')
    .fill("import time\ntime.sleep(0.2)\nresult = row.get('note', '')");
  const [, runResponse] = await Promise.all([
    page.waitForRequest((request) =>
      request.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      request.method() === 'POST',
    ),
    page.waitForResponse((response) =>
      response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      response.request().method() === 'POST',
    ),
    page.getByTestId('generated-action-run').click(),
  ]);
  const body = (await runResponse.json()) as { run_id: number | null };
  if (body.run_id == null) throw new Error('simulated v1 run did not return a run id');
  return body.run_id;
}

test('active run has prominent progress and pending AI-cell affordances', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-run-progress-visibility'));
  const rows = Array.from({ length: 24 }, (_, i) => `"row ${i} needs slow local work"`).join('\n');
  await importCsv(page.request, pid, 'rows.csv', `note\n${rows}\n`);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await startSimulatedPythonRun(page, pid);

  const banner = page.getByTestId('active-run-banner');
  await expect(banner).toBeVisible({ timeout: 15_000 });
  await expect(banner).toContainText(/queued|running|rows/i);
  await expect(page.getByTestId('run-watcher')).toBeVisible();
  await expect(page.getByTestId('pending-ai-cells')).toBeVisible();
  await expect(page.getByTestId('pending-ai-cells')).toContainText(/filling|pending|running/i);
});
