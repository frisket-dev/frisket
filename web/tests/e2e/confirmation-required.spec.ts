// Generic confirmation contract. The frontend must
// pause on ANY v1 ActionResult with status `needs_confirmation` — regardless of
// the (now generic) error code/reason, and even when the cost estimate is
// unknown — then re-POST the run with confirmed:true after the user approves.
//
// This mocks POST /run to return a generic needs_confirmation 402 whose error
// code is NOT one of the old cost-gate codes, proving the status-based parse.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  uniqueName,
} from './helpers';

const smallCsv = (rows: number): string =>
  'snippet\n' +
  Array.from(
    { length: rows },
    (_, i) => `"City agency item ${i}: routine procurement note for audit review."`,
  ).join('\n');

test('a generic needs_confirmation result trips the confirmation modal and confirms', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-confirmation'));
  await importCsv(page.request, pid, 'small.csv', smallCsv(3));

  const runPosts: Array<Record<string, unknown>> = [];
  const runRoute = `**/api/projects/${pid}/actions/v1/run`;
  // No catalog patching, and no waiting for a price. This spec used to have to
  // switch requires_confirmation off so the panel's own preflight would not
  // swallow the first unconfirmed POST. There is no preflight: every launch
  // POSTs unconfirmed, which is exactly the precondition this test needs.

  // First /run -> generic needs_confirmation 402 (NOT an old cost code, unknown
  // estimate). Second /run (confirmed) -> completed.
  await page.route(runRoute, async (route) => {
    const posted = route.request().postDataJSON() as Record<string, unknown>;
    runPosts.push(posted);
    const params = posted.params as { confirmed?: boolean } | undefined;
    if (params?.confirmed) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          action: { kind: 'map.classify', action_id: 'confirm-e2e' },
          status: 'completed',
          project_id: pid,
          run_id: 7401,
          receipt_id: 'rcpt_confirm_e2e',
          op_ids: [],
          outputs: [],
          warnings: [],
          errors: [],
        }),
      });
      return;
    }
    await route.fulfill({
      status: 402,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'map.classify', action_id: 'confirm-e2e' },
        status: 'needs_confirmation',
        project_id: pid,
        run_id: null,
        receipt_id: null,
        op_ids: [],
        outputs: [],
        warnings: [],
        errors: [
          {
            code: 'irreversible_external_requires_confirmation',
            message: 'This action needs confirmation before it runs.',
            action_kind: 'map.classify',
            field: 'params.confirmed',
            details: { reason: 'irreversible_external', estimate: { cost: null, rows: 3 } },
          },
        ],
      }),
    });
  });

  // Keep the runs/status poll from 404-ing after the confirmed run completes.
  await page.route(`**/api/projects/${pid}/actions/runs/7401/status`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 7401,
        live: false,
        status: 'completed',
        done: true,
        completed_rows: 3,
        total_rows: 3,
        failed_rows: 0,
      }),
    });
  });

  try {
    await page.goto(`/p/${pid}`);
    await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });

    await openAction(page, 'map.classify');
    await page.getByLabel('Field 1 labels').fill('routine, investigate');
    await page.getByTestId('run-button').click();

    // The generic 402 (non-cost code, unknown estimate) still trips the modal.
    const modal = page.getByTestId('cost-gate-modal');
    await expect(modal).toBeVisible({ timeout: 20_000 });

    // Approve: type the confirm word, confirm, and the run re-POSTs confirmed.
    const confirm = page.getByTestId('cost-gate-confirm');
    await expect(confirm).toBeDisabled();
    await page.getByTestId('cost-gate-input').fill('confirm');
    await expect(confirm).toBeEnabled();
    await confirm.click();

    await expect(modal).toBeHidden();
    await expect.poll(() => runPosts.length).toBe(2);

    const first = runPosts[0]?.params as { confirmed?: boolean } | undefined;
    const second = runPosts[1]?.params as { confirmed?: boolean } | undefined;
    expect(first?.confirmed ?? false).toBe(false);
    expect(second?.confirmed).toBe(true);
  } finally {
    await page.unroute(runRoute);
  }
});
