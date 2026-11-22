import { describe, expect, it, vi } from 'vitest';
import type { ActionComparisonContext, ActionComparisonPreviewRequest } from '../../src/api/actionComparison';
import type { ActionCompareCostConfirmation, ActionCompareVariant } from '../../src/workbench/actionCompareSession';
import { prepareComparisonEstimatePlan } from '../../src/workbench/evaluation/comparisonEstimatePlan';

const draft = { action_id: 'map.ask', scope: { kind: 'sheet_rows' as const, sheet_id: 7 }, params: { model: 'one' }, output_names: { answer: 'answer' } };
const context: ActionComparisonContext = {
  schema_version: 'frisket.action_compare_context.v1',
  available: true,
  reason: null,
  limits: { max_sample_rows: 500, max_challengers: 19 },
  baseline_mode: 'preview',
  comparison_basis: { kind: 'draft', draft },
  draft,
  sheet: { id: 7, name: 'Sheet' },
  sample_row_ids: Array.from({ length: 41 }, (_, index) => index + 1),
  sample_fingerprint: 'sample',
  locked_params: [],
  source_columns: [],
  source_rows: {},
  generated_columns: [],
  expected_output_signature: 'signature',
  replacement_targets: [],
  baseline_rows: null,
  origin_summary: { mixed_runs: false, includes_manual_edits: false, includes_reject_clear: false },
};
const variants: ActionCompareVariant[] = ['A', 'B'].map((id) => ({
  id,
  displayKey: id,
  label: id,
  baseline: id === 'A',
  draft: { ...draft, params: { model: id } },
  draftRevision: 0,
  status: 'pending',
  previewId: null,
  result: null,
  previousResult: null,
  error: null,
}));
const signal = () => new AbortController().signal;
const scopeRowCount = (scope: ActionComparisonPreviewRequest['action']['scope']) => (
  scope.kind === 'sheet_rows' ? (scope.row_ids?.length ?? 0) : 0
);

describe('comparison quote plan', () => {
  it('quotes all variant chunks before one approval and keeps each exact token with its request', async () => {
    const events: string[] = [];
    const estimatePreview = vi.fn(async (request: ActionComparisonPreviewRequest) => {
      events.push('quote'); return { estimate: { cost: .1, rows: scopeRowCount(request.action.scope), cost_source: 'cloud', billed_cost: 100_000, policy_id: 'policy', requires_confirmation: true, promise_set_hash: `token-${events.length}` } };
    });
    const confirm = vi.fn(async (estimate) => { events.push('confirm'); expect(estimate.billed_cost).toBe(600_000); return true; });
    const result = await prepareComparisonEstimatePlan({ context, variants, estimatePreview, confirm, signal: signal() });
    expect(events).toEqual(['quote', 'quote', 'quote', 'quote', 'quote', 'quote', 'confirm']);
    expect(result.approved).toBe(true);
    expect(result.chunks.A.map((item) => scopeRowCount(item.action.scope))).toEqual([20, 20, 1]);
    expect(result.chunks.B[2].action.confirmation).toBe('token-6');
    expect(result.chunks.A[0].action.idempotency_key).toBe(estimatePreview.mock.calls[0][0].action.idempotency_key);
  });
  it('does not authorize on rejection, missing tokens or quote errors', async () => {
    const paid = { cost: null, rows: 1, billed_cost: null, policy_id: 'p', cost_source: 'unknown', requires_confirmation: true, promise_set_hash: 'hash' };
    const confirm = vi.fn(async () => false);
    const result = await prepareComparisonEstimatePlan({ context, variants: variants.slice(0, 1), estimatePreview: async () => ({ estimate: paid }), confirm, signal: signal() });
    expect(result).toEqual({ approved: false, chunks: {} });
    expect(confirm).toHaveBeenCalledOnce();
    await expect(prepareComparisonEstimatePlan({ context, variants, estimatePreview: async () => ({ estimate: { ...paid, promise_set_hash: undefined } }), confirm, signal: signal() })).rejects.toThrow(/confirmation/i);
    await expect(prepareComparisonEstimatePlan({ context, variants, estimatePreview: async () => { throw new Error('quote failed'); }, confirm, signal: signal() })).rejects.toThrow('quote failed');
  });

  it('does not approve a plan changed while confirmation is pending', async () => {
    const edited = [{ ...variants[0], draft: structuredClone(variants[0].draft) }];
    const paid = { cost: .1, rows: 1, billed_cost: 100_000, policy_id: 'p', cost_source: 'cloud', requires_confirmation: false };
    const confirm = vi.fn(async () => { edited[0].draft.params.model = 'changed'; return true; });
    const result = await prepareComparisonEstimatePlan({ context, variants: edited, estimatePreview: async () => ({ estimate: paid }), confirm, signal: signal() });
    expect(result).toEqual({ approved: false, chunks: {} });
  });
  it('skips approval only for fully free local quotes and preserves unknown totals', async () => {
    const confirm = vi.fn<ActionCompareCostConfirmation>(async () => true);
    const free = { cost: 0, rows: 1, billed_cost: 0, policy_id: 'p', cost_source: 'free_local', requires_confirmation: false };
    const result = await prepareComparisonEstimatePlan({ context, variants: variants.slice(0, 1), estimatePreview: async () => ({ estimate: free }), confirm, signal: signal() });
    expect(result.approved).toBe(true); expect(confirm).not.toHaveBeenCalled();
    await prepareComparisonEstimatePlan({ context, variants: variants.slice(0, 1), estimatePreview: async () => ({ estimate: { ...free, cost_source: 'unknown', billed_cost: null } }), confirm, signal: signal() });
    expect(confirm.mock.calls[0][0].billed_cost).toBeNull();
  });
});
