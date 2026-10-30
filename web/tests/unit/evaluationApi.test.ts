import { afterEach, expect, it, vi } from 'vitest';
import { createEvaluationApi } from '../../src/api/evaluation';
import { createActionComparisonApi } from '../../src/api/actionComparison';

const link = { id: 1, action_id: 'map.ask', expected_sheet_id: 2, target_sheet_id: 3,
  expected_sheet_name: 'Expected', target_sheet_name: 'Source', keys: [],
  fields: [{ expected_column_id: 4, output_column_id: 5, logical_key: 'answer' }] };
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } });
afterEach(() => vi.unstubAllGlobals());
it('normalizes generated link and bounded result defaults, preserving source identities', async () => {
  const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    if (!init?.body) return json({ links: [link] });
    return json({ sheet_id: 3, action_id: 'map.ask', output_columns: [], row_ids: [7], link,
      expected: {}, expected_bounded: { 7: { answer: { preview: 'large', total_characters: 100, total_bytes: 100 } } },
      variants: [], best: {}, rule_previews: {} });
  });
  vi.stubGlobal('fetch', fetch);
  const api = createEvaluationApi((status) => new Error(String(status)), 'project');
  const links = await api.getLinks(3);
  expect(links.links[0].fields[0]).toMatchObject({ rule: 'exact', source: 'import', hidden: false });
  expect(links.links[0].hidden).toBe(false);
  const result = await api.score({ basis: { kind: 'draft', draft: { action_id: 'map.ask',
    scope: { kind: 'sheet_rows', sheet_id: 3 }, params: {}, output_names: {} } }, row_ids: [7], variants: [], link_id: 1 });
  expect(result.expected_bounded?.['7'].answer.truncated).toBe(true);
  expect(JSON.parse(fetch.mock.calls[1][1]!.body as string).basis.draft.scope).toEqual({ kind: 'sheet_rows', sheet_id: 3 });
});
it('rejects project scopes before making an evaluation request', async () => {
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
  const api = createEvaluationApi((status) => new Error(String(status)), 'project');
  expect(() => api.score({ basis: { kind: 'draft', draft: { action_id: 'map.ask',
    scope: { kind: 'project' }, params: {}, output_names: {} } }, row_ids: [7], variants: [], link_id: null })).toThrow(/sheet-row/);
  expect(fetch).not.toHaveBeenCalled();
});
it('normalizes quote null optionals while retaining billed price and confirmation', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json({ schema_version: 'frisket.action_compare_quote.v1',
    estimate: { rows: 1, cost: .01, cost_source: 'pricing_data', billed_cost: 10000, policy_id: 'p', pricing_key: null,
      requires_confirmation: true, promise_set_hash: 'exact' } })));
  const api = createActionComparisonApi((status) => new Error(String(status)), 'project');
  const quote = await api.quotePreview({ action: { action_id: 'map.ask', scope: { kind: 'sheet_rows', sheet_id: 3 },
    params: {}, output_names: {}, idempotency_key: 'one' }, comparison: { basis: { kind: 'run', run_id: 1, column_id: 2 }, expected_output_signature: 'sig' } });
  expect(quote.estimate).toMatchObject({ billed_cost: 10000, policy_id: 'p', promise_set_hash: 'exact', pricing_key: undefined });
});
