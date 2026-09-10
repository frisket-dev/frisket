import type { RunEstimate } from '../api/types';

const MICROS_PER_USD = 1_000_000;

/** What a surface should say this run costs.
 *
 *  `usd` is the figure to render (or compare); `refused` says a present
 *  estimate did not carry a complete priced verdict. That includes both an
 *  explicit Unpriceable rating and an old/malformed unrated envelope; neither
 *  may be smoothed into a provider figure or a local zero. */
export interface QuotedCost {
  usd: number | null;
  refused: boolean;
}

/** THE cost selector, and the only one: every surface that puts a price in
 *  front of a user reads this.
 *
 *  It mirrors the server's `confirmation_context.quoted_usd` exactly, because
 *  the server hashes what it quotes: a panel that renders one number while the
 *  gate hashes another is the two-surfaces defect with money in it. That is
 *  not hypothetical — it is what shipped, and under the identity policy (where
 *  provider cost and billed cost are equal) it was invisible.
 *
 *  Three states, closed at the rating boundary:
 *
 *  - `policy_id` present, `billed_cost` a number — the BILLED figure. This is
 *    what a hosted, cost-plus deployment charges, and what its consent hash
 *    binds. The open edition sends it equal to `cost`.
 *  - `policy_id` present, `billed_cost` null — the policy DECLINED to price
 *    this run. `usd` is null (render UNKNOWN) and `refused` is true, so a
 *    caller with a "free local engine" fallback knows not to apply it: showing
 *    $0.00 for a run the deployment said it could not price is worse than
 *    saying nothing.
 *  - either rating field absent or malformed — refuse the old shape. Every
 *    current 402 lane is rated, so falling back to provider `cost` here would
 *    restore the routed defect: displaying a provider figure as the amount the
 *    deployment bills without a rate authority. */
export function quotedCost(estimate: RunEstimate | null | undefined): QuotedCost {
  if (!estimate) return { usd: null, refused: false };
  if (typeof estimate.policy_id === 'string' && estimate.policy_id !== '') {
    const billed = estimate.billed_cost;
    if (typeof billed === 'number' && Number.isInteger(billed) && billed >= 0) {
      return { usd: billed / MICROS_PER_USD, refused: false };
    }
    if (billed === null) return { usd: null, refused: true };
  }
  return { usd: null, refused: true };
}

/** The figure alone, for callers with no local fallback to reconcile. */
export function quotedUsd(estimate: RunEstimate | null | undefined): number | null {
  return quotedCost(estimate).usd;
}
