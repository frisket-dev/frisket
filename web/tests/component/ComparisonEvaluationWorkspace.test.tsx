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
    setJudgment: vi.fn(async () => response),
    deleteLink: vi.fn(async () => { response.link = null; }),
    saveExpectedAnswer: vi.fn(async (input) => { response.link = link; response.expected = { 101: { answer: input.value } }; return link; }),
    setRule: vi.fn(async (id, rule) => { link.fields[0].rule = rule; return { expected_column_id: id, rule }; }),
  } as unknown as EvaluationApi;
}
async function setup({ joined = true, two = false, bestB = false, hand = false, stale = false, unavailable = false, secondField = false } = {}) {
  const response = result(joined, two);
  if (bestB) response.best.answer = ['B'];
  if (hand && response.link) {
    response.link = { ...response.link, fields: response.link.fields.map((field) => ({ ...field, rule: 'hand' })) };
    response.variants[0].verdicts['101'].answer = 'unscored';
    response.variants[0].scores.answer = { ...response.variants[0].scores.answer, remaining: 1, scored: 0, accuracy: null };
  }
  if (stale) {
    response.variants[1].rows['101'].answer.value = 'A';
    response.variants[1].verdicts['101'].answer = 'match';
    (response.variants[1] as typeof response.variants[number] & { stale_source_row_ids: number[] }).stale_source_row_ids = [101];
  }
  if (unavailable && response.link) response.link = { ...response.link, available: false, unavailable_reason: 'Sheet hidden' } as EvaluationLink;
  const api = fakeApi(response);
  const runVariant = vi.fn(async ({ onCaptured }: { onCaptured?(id: number): void }) => {
    onCaptured?.(42); return { status: 'ready' as const, previewId: 'preview-B', result: { rows: { 101: { answer: { state: 'value' as const, value: 'B' } } } } };
  });
  const context = seed();
  if (secondField) context.output_columns.push({ ...context.output_columns[0], logical_key: 'other', current_name: 'other', column_id: 14 });
  const session = createActionCompareSession({ context }, { runVariant });
  if (two) { session.addVariant(); await session.runPending(); }
  const onJoinExpected = vi.fn(); const changed = vi.fn();
  function Host({ revision = 0 }: { revision?: number }) {
    const state = useSyncExternalStore(session.subscribe, session.getSnapshot, session.getSnapshot);
    return <ComparisonEvaluationWorkspace api={api} state={state} session={session} initialLens={joined ? 'expected' : 'each_other'}
      dataRevision={revision} onJoinExpected={onJoinExpected} onExpectedAnswersChanged={changed} onOpenVariant={vi.fn()} onUseSettings={vi.fn()}
      renderGrid={(evaluation) => <button onClick={() => evaluation.onOpenRow(101)}>Open row</button>} />;
  }
  const view = render(<Host />);
  await waitFor(() => expect(api.score).toHaveBeenCalled());
  return { api, session, runVariant, response, onJoinExpected, changed, rerenderRevision: (revision: number) => view.rerender(<Host revision={revision} />) };
}

describe('comparison evaluation workspace', () => {
  it('keeps a failed expected edit open instead of changing fields', async () => {
    const { api } = await setup({ secondField: true });
    vi.mocked(api.saveExpectedAnswer).mockRejectedValue(new Error('Cannot save this edit'));
    fireEvent.click(screen.getByRole('button', { name: 'Open row' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit expected answer' })).toHaveProperty('disabled', false));
    fireEvent.click(screen.getByRole('button', { name: 'Edit expected answer' }));
    fireEvent.change(screen.getByLabelText('Expected answer'), { target: { value: 'Changed' } });
    fireEvent.click(screen.getByRole('button', { name: 'other' }));
    expect((await screen.findAllByText('Cannot save this edit')).length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: 'answer' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByLabelText('Expected answer')).toHaveProperty('value', 'Changed');
  });
  it('keeps an unavailable expected source editable and deletable without scoring its broken link', async () => {
    const { api, onJoinExpected } = await setup({ unavailable: true });
    expect(await screen.findByText(/Expected answers · Verified answers: Sheet hidden/)).toBeTruthy();
    expect(api.score).toHaveBeenLastCalledWith(expect.objectContaining({ link_id: null }));
    fireEvent.click(screen.getByRole('button', { name: 'Edit join…' }));
    expect(onJoinExpected).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: 'Remove link' }));
    await waitFor(() => expect(api.deleteLink).toHaveBeenCalledWith(1));
  });
  it('shows mixed eligibility within a shared answer and judges only the current source', async () => {
    const { api } = await setup({ two: true, hand: true, stale: true });
    fireEvent.click(await screen.findByRole('button', { name: 'Open row' }));
    expect(await screen.findByText('A · To score · B · Source changed')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Accept answer 1' }));
    await waitFor(() => expect(api.setJudgment).toHaveBeenCalledTimes(1));
    expect(api.setJudgment).toHaveBeenCalledWith(expect.objectContaining({ variant_key: 'A', row_id: 101 }));
  });
  it('refreshes on external data revision and disables stale winner actions while waiting', async () => {
    const { api, rerenderRevision } = await setup({ two: true, bestB: true });
    await screen.findByRole('button', { name: 'Use B…' });
    const initialScores = vi.mocked(api.score).mock.calls.length;
    const initialLinks = vi.mocked(api.getLinks).mock.calls.length;
    rerenderRevision(0);
    expect(api.score).toHaveBeenCalledTimes(initialScores);
    expect(api.getLinks).toHaveBeenCalledTimes(initialLinks);
    let resolve!: (response: EvaluationResponse) => void;
    vi.mocked(api.score).mockImplementation(() => new Promise((done) => { resolve = done; }));
    rerenderRevision(1);
    await waitFor(() => expect(api.score).toHaveBeenCalledTimes(initialScores + 1));
    expect(screen.getByRole('button', { name: 'Use B…' })).toHaveProperty('disabled', true);
    expect(screen.getByRole('button', { name: 'Best misses' })).toHaveProperty('disabled', true);
    expect(screen.getByRole('button', { name: 'Results' })).toHaveProperty('disabled', true);
    resolve(result(true, true));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Results' })).toHaveProperty('disabled', false));
  });
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
