// Shared run plumbing for the golden-project specs (golden-tariff,
// golden-ntsb): trigger a run through the real UI (run button + whichever
// confirmation gate appears) and wait on the real status route, failing
// LOUDLY on any terminal non-success state. No page.route anywhere — a broken
// provider key must fail a golden, never pass it.

import { expect, type APIRequestContext, type Page } from '@playwright/test';

export const GOLDEN_RUN_TIMEOUT_MS = 240_000;

/** Click Run, confirm whichever gate appears (cost or external-API — both use
 *  the cost-gate modal), and return the run_id from the real POST response. */
export async function runViaUi(page: Page, pid: string): Promise<number> {
  const runButton = page.locator(
    '[data-testid="run-button"], [data-testid="generated-action-run"]',
  );
  await expect(runButton).toBeEnabled({ timeout: 15_000 });
  const confirmedResponse = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      (() => {
        const body = response.request().postDataJSON() as {
          confirmation?: unknown;
          params?: { confirmed?: boolean };
        };
        return body?.params?.confirmed === true
          || typeof body?.confirmation === 'string';
      })(),
    { timeout: 30_000 },
  ).catch(() => null);
  const anyResponse = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      response.url().includes(`/api/projects/${pid}/actions/v1/run`),
    { timeout: 30_000 },
  );
  await runButton.click();
  // The confirmation gate can open either synchronously (frontend cost
  // preflight) or after the server's 402 needs_confirmation round-trip
  // (external-API actions like web_search) — wait for it, don't poll once.
  const gate = page.getByTestId('cost-gate-modal');
  const gateAppeared = await gate
    .waitFor({ state: 'visible', timeout: 8_000 })
    .then(() => true)
    .catch(() => false);
  if (gateAppeared) {
    await page.getByTestId('cost-gate-input').fill('confirm');
    const confirm = page.getByTestId('cost-gate-confirm');
    await expect(confirm).toBeEnabled({ timeout: 10_000 });
    await confirm.click();
  }
  // Prefer the confirmed POST when a gate re-POSTed; otherwise the first one.
  const response = (await confirmedResponse) ?? (await anyResponse);
  const body = (await response.json()) as {
    schema_version: string;
    run_id: number;
    status?: string;
    errors?: Array<{ message?: string }>;
  };
  expect(body.schema_version).toBe('frisket.action_result.v1');
  if (body.status === 'failed' || body.status === 'needs_confirmation') {
    throw new Error(`action run did not start (${body.status}): ${JSON.stringify(body.errors)}`);
  }
  expect(Number.isInteger(body.run_id)).toBe(true);
  return body.run_id;
}

/** Poll the real run status until it completes; fail LOUDLY on any terminal
 *  non-success state. `allowedFailedRows` exists only for the live-DDG search
 *  step (mirrors tests/engine/test_golden_tariff.py's one-miss tolerance). */
export async function waitForRun(
  request: APIRequestContext,
  pid: string,
  runId: number,
  label: string,
  allowedFailedRows = 0,
): Promise<void> {
  const deadline = Date.now() + GOLDEN_RUN_TIMEOUT_MS;
  for (;;) {
    const res = await request.get(`/api/projects/${pid}/actions/runs/${runId}/status`);
    expect(res.ok()).toBeTruthy();
    // The status route wraps the run: { schema_version, action, run: {...} };
    // the run's public_status carries the live flag and failed count.
    const envelope = (await res.json()) as { run?: Record<string, unknown> };
    const run = (envelope.run ?? envelope) as {
      status?: string;
      error?: string;
      failed_rows?: number;
      public_status?: { status?: string; live?: boolean; failed?: number; error?: string };
    };
    const pub = run.public_status ?? {};
    const status = String(pub.status ?? run.status ?? '');
    if (!status) {
      throw new Error(`golden ${label}: run ${runId} status response had no status field`);
    }
    if (['failed', 'cancelled', 'stalled', 'orphaned'].includes(status)) {
      const err = pub.error ?? run.error;
      throw new Error(`golden ${label}: run ${runId} ${status}${err ? `: ${err}` : ''}`);
    }
    if (status !== 'queued' && status !== 'running' && !pub.live) {
      const failed = pub.failed ?? run.failed_rows ?? 0;
      if (failed > allowedFailedRows) {
        throw new Error(`golden ${label}: run ${runId} completed with ${failed} failed rows`);
      }
      return;
    }
    if (Date.now() > deadline) {
      throw new Error(
        `golden ${label}: run ${runId} still ${status || 'live'} after ${GOLDEN_RUN_TIMEOUT_MS}ms`,
      );
    }
    await new Promise((r) => setTimeout(r, 1_000));
  }
}
