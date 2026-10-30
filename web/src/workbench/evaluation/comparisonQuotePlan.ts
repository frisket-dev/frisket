import type { ActionComparisonContext, ActionComparisonPreviewRequest } from '../../api/actionComparison';
import { freshActionRequestKey, type RunEstimate } from '../../api/types';
import type { ActionCompareCostConfirmation, ActionCompareVariant } from '../actionCompareSession';

const PREVIEW_ROWS = 20;
const clone = <T>(value: T): T => typeof structuredClone === 'function'
  ? structuredClone(value) : JSON.parse(JSON.stringify(value)) as T;

type QuotePlan = { approved: boolean; chunks: Record<string, ActionComparisonPreviewRequest[]> };

/** Quote every exact request before showing one approval or starting any work. */
export async function prepareComparisonQuotePlan({ context, variants, quote, confirm, signal }: {
  context: ActionComparisonContext;
  variants: readonly ActionCompareVariant[];
  quote(request: ActionComparisonPreviewRequest): Promise<{ estimate: RunEstimate }>;
  confirm: ActionCompareCostConfirmation;
  signal: AbortSignal;
}): Promise<QuotePlan> {
  const refused: QuotePlan = { approved: false, chunks: {} };
  const planIdentity = () => JSON.stringify({ rows: context.sample_row_ids, basis: context.comparison_basis,
    signature: context.expected_output_signature, variants: variants.map(({ id, draft }) => ({ id, draft })) });
  const initialIdentity = planIdentity();
  const changed = () => signal.aborted || planIdentity() !== initialIdentity;
  const chunks: Record<string, ActionComparisonPreviewRequest[]> = {};
  const quotes: Array<{ label: string; estimate: RunEstimate }> = [];
  for (const variant of variants) {
    if (variant.draft.scope.kind !== 'sheet_rows') throw new Error('Comparison requires a sheet-row action.');
    const requests: ActionComparisonPreviewRequest[] = [];
    for (let offset = 0; offset < context.sample_row_ids.length; offset += PREVIEW_ROWS) {
      if (changed()) return refused;
      const rowIds = context.sample_row_ids.slice(offset, offset + PREVIEW_ROWS);
      const request: ActionComparisonPreviewRequest = {
        action: { ...clone(variant.draft), scope: { ...clone(variant.draft.scope),
          row_ids: rowIds },
        idempotency_key: freshActionRequestKey(variant.draft.action_id) },
        comparison: { basis: clone(context.comparison_basis), expected_output_signature: context.expected_output_signature },
      };
      const { estimate } = await quote(clone(request));
      if (changed()) return refused;
      if (estimate.requires_confirmation && !estimate.promise_set_hash) {
        throw new Error('The comparison quote is missing its confirmation token.');
      }
      if (estimate.requires_confirmation) request.action.confirmation = estimate.promise_set_hash;
      requests.push(request);
      quotes.push({ label: `${variant.displayKey} · rows ${offset + 1}–${offset + rowIds.length}`, estimate });
    }
    chunks[variant.id] = requests;
  }
  if (!quotes.length) return refused;
  const paid = quotes.some(({ estimate }) => estimate.cost_source !== 'free_local'
    || estimate.billed_cost !== 0 || estimate.requires_confirmation || (estimate.claims?.length ?? 0) > 0);
  if (paid) {
    const amounts = quotes.map(({ estimate }) => estimate.billed_cost);
    const known = quotes.every(({ estimate }) => Boolean(estimate.policy_id)
      && typeof estimate.billed_cost === 'number' && Number.isSafeInteger(estimate.billed_cost) && estimate.billed_cost >= 0);
    const sum = known ? amounts.reduce<number>((total, value) => total + (value as number), 0) : null;
    const total: RunEstimate = {
      cost: null, rows: quotes.reduce((count, entry) => count + entry.estimate.rows, 0),
      billed_cost: sum !== null && Number.isSafeInteger(sum) ? sum : null, policy_id: 'comparison',
      claims: quotes.flatMap(({ label, estimate }, index) => (estimate.claims ?? []).map((claim) => ({
        field: `${index}:${claim.field}`, display: `${label} — ${claim.display}`,
      }))),
    };
    const accepted = await confirm(total, `Approve ${quotes.length} comparison preview${quotes.length === 1 ? '' : 's'}. Results are temporary; usage is recorded.`, signal);
    if (!accepted || changed()) return refused;
  }
  return { approved: true, chunks };
}
