// Run history must expose complete action parameters and enough identifiers
// for a user or administrator to diagnose the work. This deterministic, $0
// template run uses a fixed fixture so params/output columns/worker version are
// pinned without live model calls.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openHistory,
  openProject,
  runAndWait,
  TINY_CSV,
  uniqueName,
} from './helpers';

test('history detail panel exposes the full run record: params, id, deep link, worker version, outputs, row outcomes', async ({
  page,
  request,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async (text: string) => {
          (window as unknown as { __historyClipboard?: string }).__historyClipboard = text;
        },
      },
    });
  });

  const pid = await createProject(request, uniqueName('e2e-run-detail'));
  const sheetId = await importCsv(request, pid, 'stories.csv', TINY_CSV);
  await runAndWait(request, pid, {
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { template: { text: 'note: {{snippet}}' } },
    output_names: { rendered: 'note' },
    idempotency_key: `e2e-run-detail-template-${pid}`,
  });

  const historyResponse = await request.get(`/api/projects/${pid}/history?limit=50`);
  expect(historyResponse.ok()).toBeTruthy();
  const history = await historyResponse.json() as {
    ops: Array<{
      id: number;
      run: {
        run_id: number;
        params: Record<string, unknown>;
        output_columns: Array<{ id: number; name: string }>;
        failed_rows: number;
        total_rows: number;
        completed_rows: number;
      } | null;
    }>;
  };
  const templateOp = history.ops.at(-1);
  expect(templateOp?.run).not.toBeNull();
  const run = templateOp!.run!;
  expect(run.output_columns.map((c) => c.name)).toContain('note');
  expect(run.failed_rows).toBe(0);
  expect(run.total_rows).toBeGreaterThan(0);

  await openProject(page, pid, sheetId);
  await openHistory(page);
  const panel = page.getByTestId('bottom-dock-panel');
  const opId = templateOp!.id;

  // The run is the current op, so its detail already renders on the right.
  const detail = panel.getByTestId(`history-step-detail-op-${opId}`);
  await expect(detail).toBeVisible();

  // Params JSON: pretty-printed, contains the actual recipe fields the run
  // was launched with (not just a fraction of the record).
  const paramsBlock = panel.getByTestId(`history-run-params-${opId}`);
  await expect(paramsBlock).toBeVisible();
  await expect(paramsBlock).toContainText('"template"');
  await expect(paramsBlock).toContainText('note: {{snippet}}');
  await expect(paramsBlock).toContainText('"output_names"');

  // Output columns the run actually wrote.
  await expect(panel.getByTestId(`history-output-columns-${opId}`)).toContainText('note');

  // Row outcome summary (per-row failure summary — 0 failed here).
  await expect(panel.getByTestId(`history-row-failure-summary-${opId}`)).toContainText(
    /succeeded/,
  );

  // Worker version stamp (worker-version-guard-v1): some non-empty identity,
  // real value depends on the checkout running the e2e webServer.
  const workerVersion = panel.getByTestId(`history-worker-version-${opId}`);
  await expect(workerVersion).toBeVisible();
  await expect(workerVersion).not.toHaveText('');

  // Run id: visible and copyable.
  const runIdCode = panel.getByTestId(`history-run-id-${opId}`);
  await expect(runIdCode).toHaveText(String(run.run_id));
  await panel.getByTestId(`history-copy-run-id-${opId}`).click();
  const copiedRunId = await page.evaluate(
    () => (window as unknown as { __historyClipboard?: string }).__historyClipboard ?? '',
  );
  expect(copiedRunId).toBe(String(run.run_id));

  // Deep link: project/sheet/run URL form, present and copyable.
  const deepLinkCode = panel.getByTestId(`history-deep-link-${opId}`);
  await expect(deepLinkCode).toBeVisible();
  const deepLinkText = await deepLinkCode.textContent();
  expect(deepLinkText).toContain(`/p/${pid}/s/${sheetId}`);
  expect(deepLinkText).toContain(`run=${run.run_id}`);
  await panel.getByTestId(`history-copy-deep-link-${opId}`).click();
  const copiedDeepLink = await page.evaluate(
    () => (window as unknown as { __historyClipboard?: string }).__historyClipboard ?? '',
  );
  expect(copiedDeepLink).toBe(deepLinkText);
});
