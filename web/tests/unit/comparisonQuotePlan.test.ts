import { describe, expect, it, vi } from 'vitest';
import type { ActionComparisonContext, ActionComparisonPreviewRequest } from '../../src/api/actionComparison';
import type { ActionCompareVariant } from '../../src/workbench/actionCompareSession';
import { prepareComparisonQuotePlan } from '../../src/workbench/evaluation/comparisonQuotePlan';

const draft = { action_id: 'map.ask', scope: { kind: 'sheet_rows' as const, sheet_id: 7 }, params: { model: 'one' }, output_names: { answer: 'answer' } };
const context = { sample_row_ids: Array.from({ length: 41 }, (_, index) => index + 1), comparison_basis: { kind: 'draft', draft }, expected_output_signature: 'signature' } as ActionComparisonContext;
const variants = ['A', 'B'].map((id) => ({ id, displayKey: id, draft: { ...draft, params: { model: id } } } as ActionCompareVariant));
const signal = () => new AbortController().signal;

describe('comparison quote plan', () => {
  it('quotes all variant chunks before one approval and keeps each exact token with its request', async () => {
    const events: string[] = [];
    const quote = vi.fn(async (request: ActionComparisonPreviewRequest) => {
      events.push('quote'); return { estimate: { cost: .1, rows: request.action.scope.row_ids!.length, cost_source: 'cloud', billed_cost: 100_000, policy_id: 'policy', requires_confirmation: true, promise_set_hash: `token-${events.length}` } };
    });
    const confirm = vi.fn(async (estimate) => { events.push('confirm'); expect(estimate.billed_cost).toBe(600_000); return true; });
    const result = await prepareComparisonQuotePlan({ context, variants, quote, confirm, signal: signal() });
    expect(events).toEqual(['quote', 'quote', 'quote', 'quote', 'quote', 'quote', 'confirm']);
    expect(result.approved).toBe(true);
    expect(result.chunks.A.map((item) => item.action.scope.row_ids!.length)).toEqual([20, 20, 1]);
    expect(result.chunks.B[2].action.confirmation).toBe('token-6');
    expect(result.chunks.A[0].action.idempotency_key).toBe(quote.mock.calls[0][0].action.idempotency_key);
  });
  it('does not authorize on rejection, missing tokens or quote errors', async () => {
    const paid = { cost: null, rows: 1, billed_cost: null, policy_id: 'p', cost_source: 'unknown', requires_confirmation: true, promise_set_hash: 'hash' };
    const confirm = vi.fn(async () => false);
    const result = await prepareComparisonQuotePlan({ context, variants: variants.slice(0, 1), quote: async () => ({ estimate: paid }), confirm, signal: signal() });
    expect(result).toEqual({ approved: false, chunks: {} });
    expect(confirm).toHaveBeenCalledOnce();
    await expect(prepareComparisonQuotePlan({ context, variants, quote: async () => ({ estimate: { ...paid, promise_set_hash: undefined } }), confirm, signal: signal() })).rejects.toThrow(/confirmation/i);
    await expect(prepareComparisonQuotePlan({ context, variants, quote: async () => { throw new Error('quote failed'); }, confirm, signal: signal() })).rejects.toThrow('quote failed');
  });

  it('does not approve a plan changed while confirmation is pending', async () => {
    const edited = [{ ...variants[0], draft: structuredClone(variants[0].draft) }];
    const paid = { cost: .1, rows: 1, billed_cost: 100_000, policy_id: 'p', cost_source: 'cloud', requires_confirmation: false };
    const confirm = vi.fn(async () => { edited[0].draft.params.model = 'changed'; return true; });
    const result = await prepareComparisonQuotePlan({ context, variants: edited, quote: async () => ({ estimate: paid }), confirm, signal: signal() });
    expect(result).toEqual({ approved: false, chunks: {} });
  });
  it('skips approval only for fully free local quotes and preserves unknown totals', async () => {
    const confirm = vi.fn(async () => true);
    const free = { cost: 0, rows: 1, billed_cost: 0, policy_id: 'p', cost_source: 'free_local', requires_confirmation: false };
    const result = await prepareComparisonQuotePlan({ context, variants: variants.slice(0, 1), quote: async () => ({ estimate: free }), confirm, signal: signal() });
    expect(result.approved).toBe(true); expect(confirm).not.toHaveBeenCalled();
    await prepareComparisonQuotePlan({ context, variants: variants.slice(0, 1), quote: async () => ({ estimate: { ...free, cost_source: 'unknown', billed_cost: null } }), confirm, signal: signal() });
    expect(confirm.mock.calls[0][0].billed_cost).toBeNull();
  });
});
