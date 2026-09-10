// backfill-cost-gate-v1 (frontend): run.backfill now honors COST_GATE_USD. An
// over-gate backfill returns a 402 needs_confirmation envelope (was silent
// execution). The web client must surface the SAME priced cost-confirmation
// modal a fresh run uses, and on confirm re-POST run.backfill with
// the exact top-level confirmation token. This spec mirrors run-controls.spec.ts's backfill setup (a
// real regex run makes an AI `extracted` column, a new row leaves one cell
// incomplete, opening the column drawer exposes the backfill button) but MOCKS
// the run.backfill POST to /actions/v1/run so the 402 -> confirm -> 200 loop is
// driven deterministically.
//
// Proves the three behaviors:
//  - over the gate  -> cost-gate modal -> confirm -> echo the exact quote token
//  - cancel         -> no resubmit, no mutation, no error
//  - under the gate -> straight 200, NO modal (no regression to the common case)

import { expect, test, type Page } from '@playwright/test';
import {
  addRow,
  clickHeader,
  createProject,
  finishLocalQueuedRun,
  importCsv,
  openAction,
  selectActionInputColumns,
  sheetData,
  uniqueName,
} from './helpers';

async function startRegexRun(page: Page, pid: string): Promise<void> {
  await openAction(page, 'map.regex_extract');
  await selectActionInputColumns(page, 'note');
  await expect(page.getByTestId('field-pattern')).toBeVisible();
  await page.getByTestId('field-pattern').fill('\\$[0-9,]+');
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
  if (body.run_id == null) throw new Error('regex v1 run did not return a run id');
  finishLocalQueuedRun(pid, body.run_id);
}

/** createProject -> import -> regex run (makes `extracted`) -> add one un-run
 *  row -> open the `extracted` column drawer. Returns the ids the tests need. */
async function openBackfillDrawer(page: Page): Promise<{ pid: string; sheetId: number }> {
  const pid = await createProject(page.request, uniqueName('e2e-backfill-gate'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'note\n"contract $4,200 total"\n"fee was $96 flat"\n',
  );
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await startRegexRun(page, pid);
  await expect(page.getByTestId('run-progress')).toContainText('complete', { timeout: 30_000 });
  await expect(page.getByTestId('sheet-stats')).toHaveText('2 rows · 2 columns', {
    timeout: 30_000,
  });

  const add = await addRow(page.request, pid, sheetId, { note: 'late invoice $77' });
  expect(add.total).toBe(3);
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await page.reload();
  await expect(page.getByTestId('sheet-stats')).toHaveText('3 rows · 2 columns', {
    timeout: 15_000,
  });

  const before = await sheetData(page.request, pid, sheetId, 0, 10);
  await clickHeader(page, before.columns, 'extracted');
  await expect(page.getByTestId('column-drawer')).toBeVisible();
  return { pid, sheetId };
}

const NEEDS_CONFIRMATION_402 = (pid: string) =>
  JSON.stringify({
    schema_version: 'frisket.action_result.v1',
    action: { kind: 'run.backfill', action_id: 'backfill-e2e' },
    status: 'needs_confirmation',
    project_id: pid,
    run_id: null,
    receipt_id: null,
    op_ids: [],
    outputs: [],
    warnings: [],
    errors: [
      {
        code: 'model_cost_requires_confirmation',
        message: 'This backfill will cost about $4.20 across 900 rows.',
        action_kind: 'run.backfill',
        field: 'confirmation',
        details: {
          reason: 'model_cost', estimate: { cost: 4.2, rows: 900 },
          promise_set_hash: 'backfill-e2e-quote',
        },
      },
    ],
  });

const COMPLETED_200 = (pid: string) =>
  JSON.stringify({
    schema_version: 'frisket.action_result.v1',
    action: { kind: 'run.backfill', action_id: 'backfill-e2e' },
    status: 'completed',
    project_id: pid,
    run_id: 88001,
    receipt_id: 'rcpt_backfill_e2e',
    op_ids: [],
    outputs: [{ kind: 'run_backfill', name: null, ref: { filled: 1, run_id: 88001 } }],
    warnings: [],
    errors: [],
  });

/** Route the run.backfill POST only; everything else continues to the backend.
 *  `gate` decides whether an UNconfirmed backfill 402s (over-gate) or completes
 *  straight away (under-gate). A confirmed backfill always completes 200. */
async function routeBackfill(
  page: Page,
  pid: string,
  gate: 'over' | 'under',
  posted: Array<Record<string, unknown>>,
): Promise<void> {
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as {
      action_id?: string;
      confirmation?: string;
    };
    if (body.action_id !== 'run.backfill') {
      await route.continue();
      return;
    }
    posted.push(body);
    if (body.confirmation === 'backfill-e2e-quote' || gate === 'under') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: COMPLETED_200(pid) });
      return;
    }
    await route.fulfill({ status: 402, contentType: 'application/json', body: NEEDS_CONFIRMATION_402(pid) });
  });
}

test('over-gate backfill prompts, then echoes the exact confirmation on approval', async ({ page }) => {
  const { pid } = await openBackfillDrawer(page);
  const posted: Array<Record<string, unknown>> = [];
  await routeBackfill(page, pid, 'over', posted);

  await page.getByTestId('backfill-column-button').click();

  // The 402 trips the SHARED cost-gate modal (not a bespoke backfill dialog).
  const modal = page.getByTestId('cost-gate-modal');
  await expect(modal).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('cost-gate-estimate')).toContainText('900 rows');

  const confirm = page.getByTestId('cost-gate-confirm');
  await expect(confirm).toBeDisabled();
  await page.getByTestId('cost-gate-input').fill('confirm');
  await expect(confirm).toBeEnabled();
  await confirm.click();

  await expect(modal).toBeHidden();
  await expect(page.getByTestId('backfill-result')).toContainText('Filled 1 cell', { timeout: 20_000 });

  await expect.poll(() => posted.length).toBe(2);
  expect(posted[0]?.confirmation).toBeUndefined();
  expect(posted[1]?.confirmation).toBe('backfill-e2e-quote');
  expect(posted[1]?.idempotency_key).toBe(posted[0]?.idempotency_key);
  expect(posted[1]?.scope).toEqual(posted[0]?.scope);
  expect(posted[1]?.params).toEqual(posted[0]?.params);
});

test('cancelling the backfill cost gate runs nothing (no resubmit, no error)', async ({ page }) => {
  const { pid } = await openBackfillDrawer(page);
  const posted: Array<Record<string, unknown>> = [];
  await routeBackfill(page, pid, 'over', posted);

  await page.getByTestId('backfill-column-button').click();

  const modal = page.getByTestId('cost-gate-modal');
  await expect(modal).toBeVisible({ timeout: 20_000 });
  await page.getByTestId('cost-gate-cancel').click();
  await expect(modal).toBeHidden();

  // Only the single unconfirmed attempt was sent; no confirmed resubmit.
  await expect.poll(() => posted.length).toBe(1);
  // Clean abort: no result message and no error surfaced.
  await expect(page.getByTestId('backfill-result')).toHaveCount(0);
  // The button is usable again (busy state cleared).
  await expect(page.getByTestId('backfill-column-button')).toBeEnabled();
});

test('under-gate backfill runs with no prompt (common case, no regression)', async ({ page }) => {
  const { pid } = await openBackfillDrawer(page);
  const posted: Array<Record<string, unknown>> = [];
  await routeBackfill(page, pid, 'under', posted);

  await page.getByTestId('backfill-column-button').click();

  await expect(page.getByTestId('backfill-result')).toContainText('Filled 1 cell', { timeout: 20_000 });
  // No modal, and exactly one (unconfirmed) POST.
  await expect(page.getByTestId('cost-gate-modal')).toHaveCount(0);
  await expect.poll(() => posted.length).toBe(1);
  expect(posted[0]?.confirmation).toBeUndefined();
});
