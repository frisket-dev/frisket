// The shared cost-gate loop for run.backfill.
//
// run.backfill honors COST_GATE_USD — an over-gate backfill returns a 402
// needs_confirmation envelope (which parseJsonResponse surfaces as
// ConfirmationRequiredError) instead of silently executing, and accepts
// an exact top-level confirmation token to proceed. Both backfill callers (the workspace model's
// runActionBackfill and ColumnDrawer's runBackfill) must therefore route the
// 402 through the SAME priced confirmation surface the fresh-run path uses
// (requestCostConfirmation → the shared CostGateModal) and, on approval,
// approve the echoed token through backfillColumn. This helper factors that loop so it is not
// duplicated across the two callers.

import {
  ConfirmationRequiredError,
  type BackfillResult,
  type RunEstimate,
} from './open';
import type { ProjectApiPort } from './ports';

/** The confirmation surface both callers already hold — the run controller's
 *  `requestCostConfirmation(estimate, message)`, which presents the shared
 *  CostGateModal and resolves true on confirm, false on cancel. */
export type CostConfirmationRequester = (
  estimate: RunEstimate,
  message: string,
) => Promise<boolean>;

/**
 * Run a column backfill, gating on the server's cost estimate.
 *
 * - Under the gate (or a deterministic/$0 backfill): backfillColumn resolves
 *   directly — NO prompt — and this returns the BackfillResult.
 * - Over the gate: the api layer throws ConfirmationRequiredError; this presents
 *   the priced confirmation (via `requestConfirmation`), and on confirm
 *   re-submits with the approved quote token and returns the BackfillResult.
 * - On cancel: returns `null` — the caller must abort cleanly (no mutation, no
 *   error toast). A cancel is a user choice, not an error.
 *
 * Any non-confirmation error (a real failure) propagates so callers surface it
 * through their existing error handling.
 */
export async function backfillWithConfirmation(
  api: Pick<ProjectApiPort, 'backfillColumn'>,
  requestConfirmation: CostConfirmationRequester,
  sheetId: string,
  columnName: string,
  rowIds?: number[],
): Promise<BackfillResult | null> {
  try {
    // rowIds: a deliberate per-row retry names its exact rows; omitted, this
    // is the plain sweep. The transport preserves this distinction in scope.
    return await api.backfillColumn(sheetId, columnName, false, rowIds);
  } catch (e: unknown) {
    if (e instanceof ConfirmationRequiredError) {
      const approved = await requestConfirmation(e.estimate, e.message);
      if (!approved) return null;
      const consentedHash = e.estimate.promise_set_hash;
      return consentedHash
        ? api.backfillColumn(sheetId, columnName, true, rowIds, consentedHash)
        : api.backfillColumn(sheetId, columnName, true, rowIds);
    }
    throw e;
  }
}
