import { useEffect, useState, type ReactNode } from 'react';
import type { AttemptReceipt } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { ListDetailGrid } from '../workbench/WorkbenchListDetailPanel';
import {
  attemptReceiptsSummary,
  billedQuantityLabel,
  noChargeReason,
  quantityEvidenceLabel,
  rateLabel,
  receiptCharge,
  unsettleableLabel,
} from '../attemptReceiptModel';
import { formatUsd } from '../actions/model';

interface LoadState {
  key: string;
  receipts: AttemptReceipt[] | null;
  total: number | null;
  hasMore: boolean;
  error: string | null;
}

/** Settlement receipts for one run, or (with no `runId`) for the whole
 *  project — the UI seat for `attempt_settlement`.
 *
 *  Two rules it exists to keep:
 *
 *  1. A free or local run gains NO money row. `settle()` answers
 *     operator-borne work with an absence, not a zero, and so does this: the
 *     panel says "no charges" and stops. Only a rated settlement prints an
 *     amount.
 *  2. Whatever the server refused to compute, this refuses to fill in. A
 *     reclaimed meter or an unknown terms version renders as the reason, never
 *     as $0.00.
 *
 *  Callers own the section chrome (`h4` in the dock's detail pane, `h3` in the
 *  provenance drawer); this renders the body only, so both design languages
 *  get the same receipt.
 */
export function AttemptReceiptList({
  runId,
  limit = 25,
  testIdPrefix = 'attempt-receipt',
}: {
  runId?: number | string | null;
  limit?: number;
  testIdPrefix?: string;
}) {
  const { projectApi: api } = useWorkspaceStores();
  // The fetch key is carried IN state (MentionDetailPanel's pattern) so a
  // changed run reads as loading without a synchronous setState in the effect.
  const key = `${runId ?? ''}:${limit}`;
  const [state, setState] = useState<LoadState>({
    key,
    receipts: null,
    total: null,
    hasMore: false,
    error: null,
  });

  useEffect(() => {
    let cancelled = false;
    void api
      .listAttemptReceipts(runId ?? null, limit)
      .then((page) => {
        if (!cancelled) {
          setState({
            key,
            receipts: page.attempts,
            total: page.total,
            hasMore: page.has_more,
            error: null,
          });
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setState({
          key,
          receipts: null,
          total: null,
          hasMore: false,
          error: err instanceof Error ? err.message : 'Failed to load receipts',
        });
      });
    return () => {
      cancelled = true;
    };
  }, [key, runId, limit]);

  const current = state.key === key
    ? state
    : { key, receipts: null, total: null, hasMore: false, error: null };
  const receipts = current.receipts;
  const pageTotal = current.total;
  const hasMore = current.hasMore;
  const error = current.error;

  if (error) {
    return (
      <div className="row-field-empty" data-testid={`${testIdPrefix}-error`}>
        {error}
      </div>
    );
  }
  if (receipts === null) {
    return (
      <div className="row-field-empty" data-testid={`${testIdPrefix}-loading`}>
        Loading receipts…
      </div>
    );
  }

  const summary = attemptReceiptsSummary(receipts);
  // The no-rated-charge state. No attempt at all, or every attempt settled to
  // an absence: either way there is nothing to display as money.
  // `unavailableCount` deliberately does NOT count here — those receipts
  // render below with their reason.
  //
  // "Nothing was rated" is NOT "this was free": a run billed to the user's own
  // provider key settles to operator_borne_zero and cost them real money.
  // `noChargeReason` picks the sentence off the server-derived `borne_by`.
  const noRatedCharge =
    summary.chargedCount === 0 && summary.unavailableCount === 0;
  const hasInspectableEvidence = receipts.some(
    (receipt) =>
      receipt.target !== null ||
      receipt.consent !== null ||
      receipt.scope.length > 0,
  );
  if (noRatedCharge && !hasInspectableEvidence) {
    return (
      <div className="row-field-empty" data-testid={`${testIdPrefix}-none`}>
        {noChargeReason(summary, receipts.length > 0)}
      </div>
    );
  }

  return (
    <>
      {noRatedCharge && (
        <div className="row-field-empty" data-testid={`${testIdPrefix}-none`}>
          {noChargeReason(summary, receipts.length > 0)}
        </div>
      )}
      {summary.totalUsd !== null && (
        <div className="version-line" data-testid={`${testIdPrefix}-total`}>
          <strong>{formatUsd(summary.totalUsd)}</strong>{' '}
          <span className="muted">
            charged across {summary.chargedCount}{' '}
            {hasMore ? 'shown ' : ''}
            {summary.chargedCount === 1 ? 'attempt' : 'attempts'}
            {hasMore && pageTotal !== null ? ` · ${pageTotal} receipts total` : ''}
          </span>
        </div>
      )}
      <ol className="provenance-run-list" data-testid={`${testIdPrefix}-list`}>
        {receipts.map((receipt) => (
          <AttemptReceiptEntry
            key={receipt.attempt_id}
            receipt={receipt}
            testIdPrefix={testIdPrefix}
          />
        ))}
      </ol>
    </>
  );
}

function AttemptReceiptEntry({
  receipt,
  testIdPrefix,
}: {
  receipt: AttemptReceipt;
  testIdPrefix: string;
}) {
  const charge = receiptCharge(receipt);
  const settlement = receipt.settlement;
  const rows: Array<[string, ReactNode]> = [];
  // Element row values carry a key off the receipt's own identity plus the row
  // label — stable under any reordering, and never positional.
  const rowKey = (label: string) => `${receipt.attempt_id}:${label}`;

  if (receipt.target) {
    const target = [
      receipt.target.target_id,
      receipt.target.engine,
      receipt.target.operator,
      receipt.target.transport,
      receipt.target.credential_source,
    ].filter((value): value is string => typeof value === 'string' && value.length > 0);
    rows.push(['Target', target.join(' · ')]);
  }
  if (receipt.scope.length > 0) {
    rows.push(['Scope', receipt.scope.join(', ')]);
  }

  if (charge.kind === 'charged') {
    rows.push(['Charged', <strong key={rowKey('Charged')}>{formatUsd(charge.amountUsd)}</strong>]);
  } else if (charge.kind === 'unavailable') {
    rows.push([
      'Charged',
      <span key={rowKey('Charged')} className="muted">
        not available — {unsettleableLabel(charge.reason)}
      </span>,
    ]);
  } else if (charge.kind === 'free') {
    rows.push([
      'Charged',
      <span key={rowKey('Charged')} className="muted">
        nothing — operator-borne
      </span>,
    ]);
  } else if (charge.kind === 'unpriced') {
    rows.push([
      'Charged',
      <span key={rowKey('Charged')} className="muted">
        unknown — this attempt carried no priceable cost basis
      </span>,
    ]);
  } else {
    rows.push([
      'Charged',
      <span key={rowKey('Charged')} className="muted">
        nothing settled — the attempt was never admitted
      </span>,
    ]);
  }

  if (settlement) {
    const quantity = billedQuantityLabel(settlement);
    if (quantity) rows.push(['Quantity', quantity]);
    if (
      settlement.rated_charge_usd != null &&
      settlement.rated_charge_usd !== settlement.charge_usd
    ) {
      const rated = Number(settlement.rated_charge_usd);
      if (Number.isFinite(rated)) {
        rows.push(['Rated value', formatUsd(rated)]);
      }
    }
    if (
      settlement.charged_quantity != null &&
      settlement.quantity_unit &&
      settlement.charged_quantity !== settlement.billable_quantity
    ) {
      rows.push([
        'Charged quantity',
        `${settlement.charged_quantity} ${settlement.quantity_unit}`,
      ]);
    }
    if (settlement.absorbed_overage_usd != null) {
      const absorbed = Number(settlement.absorbed_overage_usd);
      if (Number.isFinite(absorbed) && absorbed > 0) {
        rows.push(['Absorbed overage', formatUsd(absorbed)]);
      }
    }
    if (settlement.metered_quantity != null && settlement.metered_unit) {
      const metered = `${settlement.metered_quantity} ${settlement.metered_unit}`;
      rows.push(['Metered', quantityEvidenceLabel(metered, settlement.unmetered_calls)]);
    }
    const rate = rateLabel(settlement);
    if (rate) rows.push(['Rate', rate]);
    if (settlement.pricing_key) {
      rows.push([
        'Pricing key',
        <code key={rowKey('Pricing key')}>{settlement.pricing_key}</code>,
      ]);
    }
    if (settlement.price_card_version) {
      rows.push([
        'Terms version',
        <code key={rowKey('Terms version')}>{settlement.price_card_version}</code>,
      ]);
    }
    if (settlement.consented_quantity != null && settlement.quantity_unit) {
      rows.push([
        'Consented',
        <>
          {settlement.consented_quantity} {settlement.quantity_unit}
          {settlement.exceeds_consented && (
            <span className="bottom-dock-row-error-count"> · exceeded</span>
          )}
        </>,
      ]);
    }
    if (settlement.unmetered_calls) {
      rows.push(['Unmetered calls', String(settlement.unmetered_calls)]);
    }
  } else if (receipt.price_card_version) {
    rows.push([
      'Terms version',
      <code key={rowKey('Terms version')}>{receipt.price_card_version}</code>,
    ]);
  }

  if (receipt.consent) {
    rows.push([
      'Consent',
      <>
        <code>{receipt.consent.id}</code>{' '}
        <span className="muted">({receipt.consent.grant_basis ?? 'unknown basis'})</span>
      </>,
    ]);
  }
  // Ruling 7's orphan, said plainly rather than as a missing field.
  rows.push([
    'Run',
    receipt.run_id == null ? (
      <span key={rowKey('Run')} className="muted" data-testid={`${testIdPrefix}-orphan`}>
        reclaimed — the run’s data was compacted; this record outlived it
      </span>
    ) : (
      String(receipt.run_id)
    ),
  ]);

  return (
    <li data-testid={`${testIdPrefix}-row`} data-attempt-id={receipt.attempt_id}>
      <div className="version-line">
        <span className="col-field-name">attempt {receipt.seq}</span>
        <span className="muted">{receipt.state}</span>
        <span className="muted">{receipt.attempt_id}</span>
      </div>
      <ListDetailGrid rows={rows} />
    </li>
  );
}
