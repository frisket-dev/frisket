// @vitest-environment jsdom
import { useSyncExternalStore } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ActionComparisonContext } from '../../src/api/actionComparison';
import type { EvaluationApi, EvaluationLink, EvaluationResponse } from '../../src/api/evaluation';
import { createActionCompareSession } from '../../src/workbench/actionCompareSession';
import { ComparisonEvaluationWorkspace } from '../../src/workbench/evaluation/ComparisonEvaluationWorkspace';

vi.mock('../../src/workbench/evaluation/ComparisonSources', () => ({ ComparisonSources: () => <div>Normal source pane</div> }));
vi.mock('../../src/workbench/evaluation/ComparisonAnswerValue', () => ({ ComparisonAnswerValue: ({ value }: { value: unknown }) => <span>{String(value ?? '')}</span> }));
afterEach(cleanup);

const link: EvaluationLink = { id: 1, target_sheet_id: 7, expected_sheet_id: 8,
  action_id: 'map.extract', keys: [{ expected_column_id: 20, target_column_id: 10 }], hidden: false,
  expected_sheet_name: 'Verified answers', target_sheet_name: 'Cases',
  fields: [{ expected_column_id: 21, output_column_id: 13, logical_key: 'answer', rule: 'exact', hidden: false, source: 'import' }] };

function seed(): ActionComparisonContext {
  const draft = { action_id: 'map.extract', scope: { kind: 'sheet_rows' as const, sheet_id: 7 },
    params: { model: 'local/qwen', instruction: 'Extract' }, output_names: { answer: 'answer' } };
  return { schema_version: 'frisket.action_compare_context.v1', available: true, reason: null,
    limits: { max_sample_rows: 20, max_challengers: 3 }, baseline_mode: 'current_values',
    comparison_basis: { kind: 'run', run_id: 41, column_id: 13 }, draft, sheet: { id: 7, name: 'Cases' },
    sample_row_ids: [101], sample_fingerprint: 'inputs', locked_params: [], source_columns: [], source_rows: {},
    output_columns: [{ logical_key: 'answer', column_id: 13, current_name: 'answer', column_type: 'text', format: null, hidden: false, schema: null }],
    expected_output_signature: 'output', replacement_targets: [{ logical_key: 'answer', current_name: 'answer', column_id: 13 }],
    baseline_rows: { 101: { answer: { state: 'value', value: 'A', origin: { kind: 'run_result', run_id: 41, op_id: 9, review_decision: null } } } },
    origin_summary: { mixed_runs: false, includes_manual_edits: false, includes_reject_clear: false },
  };
}
function result(joined = true, two = false): EvaluationResponse {
  const score = { checked: joined ? 1 : 0, attempted: 1, matches: 1, misses: 0, failed: 0, scored: 1, remaining: 0, accuracy: 1, coverage: 1 };
  return { sheet_id: 7, action_id: 'map.extract', output_columns: [{ logical_key: 'answer', column_id: 13, current_name: 'answer' }], row_ids: [101],
    link: joined ? link : null, expected: joined ? { 101: { answer: 'A' } } : {}, best: { answer: ['A'] }, rule_previews: {},
    variants: [{ key: 'A', label: 'Current values', source: { kind: 'current' }, settings: {}, rows: { 101: { answer: { state: 'value', value: 'A' } } }, source_fingerprints: {}, verdicts: { 101: { answer: joined ? 'match' : 'unchecked' } }, scores: { answer: score } },
      ...(two ? [{ key: 'B', label: 'B', source: { kind: 'saved' as const, saved_id: 42 }, settings: {}, rows: { 101: { answer: { state: 'value' as const, value: 'B' } } }, source_fingerprints: {}, verdicts: { 101: { answer: joined ? 'miss' as const : 'unchecked' as const } }, scores: { answer: { ...score, matches: 0, misses: joined ? 1 : 0, accuracy: 0 } } }] : [])] };
}
function fakeApi(response: EvaluationResponse): EvaluationApi {
  return { getLinks: vi.fn(async () => ({ links: response.link ? [response.link] : [] })), getShelf: vi.fn(async () => ({ items: [] })),
    score: vi.fn(async () => response), saveShelf: vi.fn(async () => ({ items: [] })),
    saveExpectedAnswer: vi.fn(async (input) => { response.link = link; response.expected = { 101: { answer: input.value } }; return link; }),
    setRule: vi.fn(async (id, rule) => { link.fields[0].rule = rule; return { expected_column_id: id, rule }; }),
  } as unknown as EvaluationApi;
}
async function setup({ joined = true, two = false } = {}) {
  const response = result(joined, two);
  const api = fakeApi(response);
  const runVariant = vi.fn(async ({ onCaptured }: { onCaptured?(id: number): void }) => {
    onCaptured?.(42); return { status: 'ready' as const, previewId: 'preview-B', result: { rows: { 101: { answer: { state: 'value' as const, value: 'B' } } } } };
  });
  const session = createActionCompareSession({ context: seed() }, { runVariant });
  if (two) { session.addVariant(); await session.runPending(); }
  const onJoinExpected = vi.fn(); const changed = vi.fn();
  function Host() {
    const state = useSyncExternalStore(session.subscribe, session.getSnapshot, session.getSnapshot);
    return <ComparisonEvaluationWorkspace api={api} state={state} session={session} initialLens={joined ? 'expected' : 'each_other'}
      onJoinExpected={onJoinExpected} onExpectedAnswersChanged={changed} onOpenVariant={vi.fn()} onUseSettings={vi.fn()}
      renderGrid={(evaluation) => <button onClick={() => evaluation.onOpenRow(101)}>Open row</button>} />;
  }
  render(<Host />);
  await waitFor(() => expect(api.score).toHaveBeenCalled());
  return { api, session, runVariant, response, onJoinExpected, changed };
}

describe('comparison evaluation workspace', () => {
  it('scores a current-values baseline without executing an action', async () => {
    const { api, runVariant } = await setup();
    await screen.findByRole('button', { name: 'Results' });
    expect(api.score).toHaveBeenLastCalledWith(expect.objectContaining({ row_ids: [101], variants: [expect.objectContaining({ key: 'A', source: { kind: 'current' } })] }));
    expect(runVariant).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Open row' }));
    fireEvent.click(screen.getByRole('button', { name: 'To score · 0' }));
    expect(await screen.findByText('No answers left to score.')).toBeTruthy();
  });
  it('shelves live durable captures together with the current baseline', async () => {
    const { api } = await setup({ two: true });
    fireEvent.click(await screen.findByRole('button', { name: '🗄 Shelve' }));
    await waitFor(() => expect(api.saveShelf).toHaveBeenCalledWith(expect.objectContaining({ variant_keys: ['A', 'B'] })));
  });
  it('shows shelf items only for the same physical output family', async () => {
    const { api } = await setup();
    const summary = { id: 9, label: 'This output family', sheet_id: 7, action_id: 'map.extract', row_count: 1,
      output_columns: [{ logical_key: 'answer', column_id: 13, current_name: 'renamed answer' }], saved_at: '2026-10-30T00:00:00Z' };
    vi.mocked(api.getShelf).mockResolvedValue({ items: [summary, { ...summary, id: 10, label: 'Another output family', output_columns: [{ logical_key: 'answer', column_id: 99, current_name: 'answer' }] }] });
    fireEvent.click(screen.getByRole('button', { name: '✓ Expected answers' }));
    await waitFor(() => expect(api.getShelf).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: '+ ⌄' }));
    expect(await screen.findByRole('menuitem', { name: 'This output family · 1 rows' })).toBeTruthy();
    expect(screen.queryByText(/Another output family/)).toBeNull();
  });
  it('keeps rejection local and creates expected answers only when an answer is accepted', async () => {
    const { api, changed } = await setup({ joined: false, two: true });
    fireEvent.click(await screen.findByRole('button', { name: 'Variants disagree · 1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reject answer 1' }));
    expect(api.saveExpectedAnswer).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Accept answer 2' }));
    await waitFor(() => expect(api.saveExpectedAnswer).toHaveBeenCalledWith(expect.objectContaining({ row_id: 101, logical_key: 'answer', value: 'B', rule: 'exact' })));
    expect(changed).toHaveBeenCalled();
  });
});
