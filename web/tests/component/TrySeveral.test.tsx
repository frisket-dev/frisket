// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useSyncExternalStore } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { EvaluationResponse } from '../../src/api/evaluation';
import type { ActionCompareSession, ActionCompareSessionSnapshot, ActionCompareVariant } from '../../src/workbench/actionCompareSession';
import { TrySeveral } from '../../src/workbench/evaluation/TrySeveral';
import { installDialogPolyfill } from '../support/domPolyfills';
import type { GridViewportProps } from '../../src/grid/GridViewport';

installDialogPolyfill();



vi.mock('../../src/grid/GridViewport', () => ({
  GridViewport: ({ columns, rows, getCellContent, onCellClicked }: Pick<GridViewportProps, 'columns' | 'rows' | 'getCellContent' | 'onCellClicked'>) => <div data-testid="matrix">
    {Array.from({ length: rows }, (_, row) => columns.map((column: { title: string }, col: number) =>
      <button type="button" key={`${row}-${col}`} onClick={() => onCellClicked?.([col, row], {} as never)}>
        {column.title}: {String((getCellContent([col, row]) as { displayData?: string }).displayData ?? '')}
      </button>))}
  </div>,
}));

afterEach(cleanup);

function fixture(capacity = 8, response: EvaluationResponse | null = null, modelField = 'model') {
  const draft = { action_id: 'map.extract', scope: { kind: 'sheet_rows', sheet_id: 1 },
    params: { source: 'body', model: 'model-a', instructions: 'original', ...(modelField === 'engine' ? { engine: 'local', execution_target: 'local', provider_ref: 'old-provider' } : {}) }, output_names: { result: 'result' } };
  let snapshot = { context: { available: true, limits: { max_challengers: capacity } },
    variants: [{ id: 'baseline', displayKey: 'A', label: 'Current', baseline: true, draft, status: 'current' }],
    sequenceStatus: 'idle' } as unknown as ActionCompareSessionSnapshot;
  const listeners = new Set<() => void>();
  const publish = (variants: ActionCompareVariant[]) => { snapshot = { ...snapshot, variants }; listeners.forEach(fn => fn()); };
  const runPending = vi.fn(async () => { publish(snapshot.variants.map(v => v.baseline ? v : { ...v, status: 'ready' })); });
  const session = {
    getSnapshot: () => snapshot,
    subscribe: (fn: () => void) => { listeners.add(fn); return () => listeners.delete(fn); },
    addVariant: () => {
      const id = `v${snapshot.variants.length}`;
      publish([...snapshot.variants, { ...snapshot.variants[0], id, displayKey: String.fromCharCode(65 + snapshot.variants.length), baseline: false, status: 'pending' }]);
      return id;
    },
    updateVariant: (id, next) => { publish(snapshot.variants.map(v => v.id === id ? { ...v, draft: next } : v)); return true; },
    removeVariant: (id) => { publish(snapshot.variants.filter(v => v.id !== id)); return true; },
    runPending,
    useVariant: (id: string) => ({ variantId: id }),
  } as unknown as ActionCompareSession;
  const onOpenVariant = vi.fn();
  const onUse = vi.fn();
  const Editor = ({ session: live }: { session: ActionCompareSession }) => {
    const state = useSyncExternalStore(live.subscribe, live.getSnapshot, live.getSnapshot);
    return <TrySeveral session={live} state={state} response={response} modelField={modelField} instructionsField="instructions"
      onOpenVariant={onOpenVariant} onUse={onUse} onClose={vi.fn()}
      renderVariantEditor={({ label, draft: value, onDraftChange, lockedParams }) => <label>{label}
        <input aria-label={label} value={String(value.params[modelField])}
          onChange={event => onDraftChange({ ...value, params: { ...value.params, [modelField]: event.target.value, source: 'tampered',
            ...(modelField === 'engine' ? { model: 'chosen/model', execution_target: 'remote', provider_ref: 'chosen-provider' } : {}) } })} />
        <span>{lockedParams.join(',')}</span>
      </label>} />;
  };
  return { session, runPending, Editor, onOpenVariant, onUse };
}

describe('Try several', () => {
  it('keeps the complete engine and model choice from the normal editor', () => {
    const { session, Editor } = fixture(8, null, 'engine');
    render(<Editor session={session} />);
    expect(screen.getByRole('dialog', { name: 'Try several values' })).toBeVisible();
    fireEvent.change(screen.getByLabelText('Model 1'), { target: { value: 'litellm' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run 1 combinations' }));
    expect(session.getSnapshot().variants[1].draft.params).toMatchObject({ engine: 'litellm', model: 'chosen/model',
      execution_target: 'remote', provider_ref: 'chosen-provider', source: 'body', instructions: 'original' });
  });
  it('creates all model and instruction combinations using the normal run', () => {
    const { session, runPending, Editor } = fixture();
    render(<Editor session={session} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add model' }));
    fireEvent.change(screen.getByLabelText('Model 2'), { target: { value: 'model-b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add instructions' }));
    fireEvent.change(screen.getByLabelText('Instructions 2'), { target: { value: 'alternative' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add instructions' }));
    fireEvent.change(screen.getByLabelText('Instructions 3'), { target: { value: 'third' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run 6 combinations' }));
    expect(session.getSnapshot().variants.slice(1).map(v => [v.draft.params.model, v.draft.params.instructions, v.draft.params.source])).toEqual([
      ['model-a', 'original', 'body'], ['model-a', 'alternative', 'body'], ['model-a', 'third', 'body'],
      ['model-b', 'original', 'body'], ['model-b', 'alternative', 'body'], ['model-b', 'third', 'body'],
    ]);
    expect(runPending).toHaveBeenCalledOnce();
  });

  it('skips an unchecked combination and stays within capacity', () => {
    const { session, runPending, Editor } = fixture(1);
    render(<Editor session={session} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add model' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Model 2 · Instructions 1' }));
    expect(screen.getByText('1 selected · 1 skipped')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Run 1 combinations' }));
    expect(session.getSnapshot().variants).toHaveLength(2);
    expect(runPending).toHaveBeenCalledOnce();
    expect(screen.getByText(/Skipped/)).toBeInTheDocument();
  });

  it('shows exact ties for equal coverage and does not select an unfinished hand field', () => {
    const score = { checked: 2, attempted: 2, matches: 2, misses: 0, failed: 0, scored: 2, remaining: 0, accuracy: 1, coverage: 1 };
    const response = {
      row_ids: [10, 11],
      link: { fields: [ { logical_key: 'result', rule: 'exact' }, { logical_key: 'essay', rule: 'hand' } ] },
      variants: [
        { key: 'B', scores: { result: score, essay: { ...score, remaining: 1 } }, verdicts: { '10': { result: 'match' }, '11': { result: 'match' } } },
        { key: 'C', scores: { result: score, essay: { ...score, remaining: 1 } }, verdicts: { '10': { result: 'match' }, '11': { result: 'match' } } },
      ],
    } as unknown as EvaluationResponse;
    const { session, Editor, onOpenVariant, onUse } = fixture(8, response);
    render(<Editor session={session} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add model' }));
    fireEvent.click(screen.getByRole('button', { name: 'Run 2 combinations' }));
    expect(screen.getAllByText(/100% ★/)).toHaveLength(2);
    fireEvent.click(screen.getAllByText(/100% ★/)[1]);
    expect(onOpenVariant).toHaveBeenCalledWith('v2');
    fireEvent.click(screen.getByRole('button', { name: 'Use best' }));
    expect(onUse).toHaveBeenCalledWith({ variantId: 'v1' });
    expect(screen.getByRole('option', { name: 'essay' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Use best' })).toBeEnabled();
  });

  it('does not choose a winner from unequal coverage', () => {
    const score = { checked: 2, attempted: 2, matches: 2, misses: 0, failed: 0, scored: 2, remaining: 0, accuracy: 1, coverage: 1 };
    const response = { row_ids: [10, 11], link: { fields: [{ logical_key: 'result', rule: 'exact' }] }, variants: [
      { key: 'B', scores: { result: score }, verdicts: { '10': { result: 'match' }, '11': { result: 'match' } } },
      { key: 'C', scores: { result: { ...score, attempted: 1, accuracy: 1, coverage: 0.5 } }, verdicts: { '10': { result: 'match' }, '11': { result: 'not_run' } } },
    ] } as unknown as EvaluationResponse;
    const { session, Editor } = fixture(8, response);
    render(<Editor session={session} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add model' }));
    fireEvent.click(screen.getByRole('button', { name: 'Run 2 combinations' }));
    expect(screen.queryByText(/★/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Use best' })).toBeDisabled();
  });

  it('does not choose a winner when equal scores cover different rows', () => {
    const score = { checked: 2, attempted: 1, matches: 1, misses: 0, failed: 0, scored: 1, remaining: 0, accuracy: 1, coverage: 0.5 };
    const response = { row_ids: [10, 11], link: { fields: [{ logical_key: 'result', rule: 'exact' }] }, variants: [
      { key: 'B', scores: { result: score }, verdicts: { '10': { result: 'match' }, '11': { result: 'not_run' } } },
      { key: 'C', scores: { result: score }, verdicts: { '10': { result: 'not_run' }, '11': { result: 'match' } } },
    ] } as unknown as EvaluationResponse;
    const { session, Editor } = fixture(8, response);
    render(<Editor session={session} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add model' }));
    fireEvent.click(screen.getByRole('button', { name: 'Run 2 combinations' }));
    expect(screen.queryByText(/★/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Use best' })).toBeDisabled();
  });

  it('does not create or run combinations over capacity', () => {
    const { session, runPending, Editor } = fixture(1);
    render(<Editor session={session} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add model' }));
    expect(screen.getByRole('button', { name: 'Run 2 combinations' })).toBeDisabled();
    expect(screen.getByText(/Only 1 .* available/)).toBeInTheDocument();
    expect(session.getSnapshot().variants).toHaveLength(1);
    expect(runPending).not.toHaveBeenCalled();
  });
});
