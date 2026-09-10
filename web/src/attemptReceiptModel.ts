/** Reading an attempt receipt honestly.
 *
 *  The server's `settle()` deliberately produces four different answers to
 *  "what did it cost", and three of them are NOT a number. This module is the
 *  one place that classifies them, so no surface can turn "no charge was ever
 *  rated" into a $0.00 line item — the same fabrication the server refuses to
 *  produce (`charge_usd: null` rather than a confident zero).
 */

import type { AttemptReceipt, AttemptSettlement } from './api/open';
import { providerLabel } from './actions/model';

export type ReceiptCharge =
  /** A rated charge. `amountUsd` may legitimately be 0 (a priced SKU that
   *  metered nothing); it is still a settlement the server computed. */
  | { kind: 'charged'; amountUsd: number; raw: string }
  /** Operator-borne zero: the free/local run. An ABSENCE of a charge, which
   *  is why it must never render as money. */
  | { kind: 'free' }
  /** The attempt carried no cost basis this build could price. */
  | { kind: 'unpriced' }
  /** The attempt was never admitted, so no terms version was pinned and there is
   *  nothing to settle. */
  | { kind: 'not_settled' }
  /** Settlement refused to guess: metering may be reclaimed or incomplete,
   *  or pinned terms may be unavailable to this build. */
  | { kind: 'unavailable'; reason: string };

export function receiptCharge(receipt: AttemptReceipt): ReceiptCharge {
  const settlement = receipt.settlement;
  if (!settlement) return { kind: 'not_settled' };
  if (settlement.unsettleable) {
    return { kind: 'unavailable', reason: settlement.unsettleable };
  }
  if (settlement.charge_usd == null) return { kind: 'unpriced' };
  // `pricing_key` is null only for operator_borne_zero, whose "0" is the
  // absence of a charge rather than a rated one.
  if (settlement.pricing_key == null) return { kind: 'free' };
  const amount = Number(settlement.charge_usd);
  if (!Number.isFinite(amount)) return { kind: 'unpriced' };
  return { kind: 'charged', amountUsd: amount, raw: settlement.charge_usd };
}

export const UNSETTLEABLE_LABELS: Record<string, string> = {
  metering_reclaimed:
    'the run’s metering was reclaimed by compaction, so the charge cannot be recomputed',
  // Retained wire spelling for receipts minted by older servers.
  unknown_price_card: 'this build cannot decode the terms the run settled under',
  settlement_terms_missing: 'the pinned settlement terms are incomplete',
  terms_version_mismatch: 'the pinned basis and attempt terms versions do not match',
  unmetered:
    'valid metering is missing for all or part of this priced attempt, so the total cannot be rated',
  consent_ceiling_missing:
    'the pinned charge ceiling is missing or invalid, so no charge was computed',
};

export function unsettleableLabel(reason: string): string {
  return UNSETTLEABLE_LABELS[reason] ?? reason;
}

export interface AttemptReceiptsSummary {
  /** Receipts that produced a rated charge. */
  chargedCount: number;
  /** Sum of the rated charges, or null when nothing was rated — never 0. */
  totalUsd: number | null;
  /** Receipts whose charge the server refused to guess at. */
  unavailableCount: number;
  orphanCount: number;
  /** Providers whose own key bore this work — the union of the receipts'
   *  server-derived `borne_by`. Non-empty means "the platform charged nothing"
   *  is true and "this cost you nothing" is FALSE. */
  credentialedProviders: string[];
  /** Whether any receipt could say who bore the work at all. False when every
   *  `borne_by` is null (no metering to read), which must not be reported as
   *  a free run. */
  bornByKnown: boolean;
}

export function attemptReceiptsSummary(
  receipts: readonly AttemptReceipt[],
): AttemptReceiptsSummary {
  let chargedCount = 0;
  let total = 0;
  let unavailableCount = 0;
  let orphanCount = 0;
  let bornByKnown = false;
  const credentialed = new Set<string>();
  for (const receipt of receipts) {
    if (receipt.run_id == null) orphanCount += 1;
    const charge = receiptCharge(receipt);
    if (charge.kind === 'charged') {
      chargedCount += 1;
      total += charge.amountUsd;
    } else if (charge.kind === 'unavailable') {
      unavailableCount += 1;
    }
    const borneBy = receipt.borne_by;
    if (borneBy) {
      bornByKnown = true;
      for (const provider of borneBy.credentialed_providers) credentialed.add(provider);
    }
  }
  return {
    chargedCount,
    totalUsd: chargedCount > 0 ? total : null,
    unavailableCount,
    orphanCount,
    credentialedProviders: [...credentialed].sort(),
    bornByKnown,
  };
}

/** What the panel may honestly say when nothing was rated.
 *
 *  Three different facts used to share one sentence ("No charges — this ran at
 *  no cost to you"), and for a run billed to the user's own provider key that
 *  sentence was simply false. */
export function noChargeReason(
  summary: AttemptReceiptsSummary,
  hasReceipts: boolean,
): string {
  if (!hasReceipts) return 'No execution attempts recorded.';
  if (summary.credentialedProviders.length > 0) {
    const providers = summary.credentialedProviders.map(providerLabel);
    const named =
      providers.length === 1
        ? providers[0]
        : `${providers.slice(0, -1).join(', ')} and ${providers[providers.length - 1]}`;
    return `No platform charge — this ran on your own ${named} key, and that account was billed.`;
  }
  if (!summary.bornByKnown) {
    // Metering reclaimed, or a run that recorded no calls. We know no charge
    // was rated; we do NOT know the run cost nothing.
    return 'No charges rated for this run.';
  }
  return 'No charges — this ran locally, at no cost.';
}

/** Qualify meter evidence without turning a legacy-absent completeness marker
 *  into an asserted zero. Current settlements always carry the count, but the
 *  wire type remains optional so old receipts can still be read. */
export function quantityEvidenceLabel(
  quantity: string,
  unmeteredCalls: number | undefined,
): string {
  if (unmeteredCalls === 0) return quantity;
  if (typeof unmeteredCalls === 'number' && unmeteredCalls > 0) {
    return `at least ${quantity}`;
  }
  return `at least ${quantity} (meter completeness unknown)`;
}

/** The quantity line: what was billed, in the unit it was quoted in. Null when
 *  settlement rated nothing, so the panel omits the row rather than printing
 *  a bare unit with no number. */
export function billedQuantityLabel(settlement: AttemptSettlement): string | null {
  if (settlement.billable_quantity == null) return null;
  const unit = settlement.quantity_unit;
  const quantity = unit
    ? `${settlement.billable_quantity} ${unit}`
    : settlement.billable_quantity;
  return quantityEvidenceLabel(quantity, settlement.unmetered_calls);
}

/** The rate line, in the server's own vocabulary. Base has no published /
 *  cost-plus rating distinction, so what is named here is the SKU the attempt
 *  pinned and the rate it pinned with it — nothing is inferred. */
export function rateLabel(settlement: AttemptSettlement): string | null {
  if (settlement.unit_rate == null || settlement.quantity_unit == null) return null;
  return `$${settlement.unit_rate} / ${settlement.quantity_unit}`;
}
