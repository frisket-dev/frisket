// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { PreviewSampleResult, RunEstimate } from '../../src/api/types';
import type { CompareColumn, ScratchDoc } from '../../src/workbench/mediaCompareSession';
import { usePaidMediaComparison } from '../../src/workbench/usePaidMediaComparison';

const doc: ScratchDoc<string> = { id: 'upload', file: new File(['audio'], 'clip.wav'),
  filename: 'clip.wav', objectUrl: 'blob:test', mediaKind: 'audio', pages: [], pageCount: null, runs: {}, votes: {} };
const columns: CompareColumn[] = ['one', 'two'].map((id) => ({ id, engineId: id, options: {} }));
const quote = (engine: string, price: number | null = 1250): RunEstimate => ({
  cost: 0.001, rows: 1, billed_cost: price, policy_id: 'hosted', requires_confirmation: true,
  promise_set_hash: engine.repeat(64).slice(0, 64),
});
const done = { status: 'done', kind: 'table', rows: [], error: null } as unknown as PreviewSampleResult;

function setup(estimate = vi.fn(async (_file: File, input: { engine: string }) => quote(input.engine))) {
  const start = vi.fn(async () => ({ previewId: 'preview', total: 1 }));
  const api = { getPreview: vi.fn().mockResolvedValue(done), cancelPreview: vi.fn().mockResolvedValue(undefined) };
  const result = renderHook(() => usePaidMediaComparison({
    api, inputFor: (_doc: ScratchDoc<string>, column: CompareColumn) => ({ engine: column.engineId!, time_limit_seconds: 600 }),
    estimate, start, readResult: () => 'transcript',
  }));
  return { ...result, start, estimate, api };
}

describe('paid upload comparisons', () => {
  it('quotes all variants before one confirmation and carries each exact token into its start', async () => {
    const { result, start, estimate } = setup();
    const signal = new AbortController().signal;
    let preparing!: Promise<boolean>;
    act(() => { preparing = result.current.prepareRun(columns.map((column) => ({ doc, column })), signal); });
    await waitFor(() => expect(result.current.gate).toBeTruthy());
    expect(estimate).toHaveBeenCalledTimes(2);
    expect(start).not.toHaveBeenCalled();
    expect(result.current.gate!.props.estimate.billed_cost).toBe(2500);
    act(() => result.current.gate!.props.onConfirm());
    expect(await preparing).toBe(true);
    for (const column of columns) await result.current.runColumn(doc, column, undefined, signal);
    expect(start.mock.calls).toEqual(columns.map((column) => [doc.file, {
      engine: column.engineId, time_limit_seconds: 600, confirmation: quote(column.engineId!).promise_set_hash,
    }]));
  });

  it('declining or aborting confirmation starts nothing', async () => {
    const { result, start } = setup();
    for (const abort of [false, true]) {
      const controller = new AbortController();
      let preparing!: Promise<boolean>;
      act(() => { preparing = result.current.prepareRun([{ doc, column: columns[0] }], controller.signal); });
      await waitFor(() => expect(result.current.gate).toBeTruthy());
      act(() => { if (abort) controller.abort(); else result.current.gate!.props.onCancel(); });
      expect(await preparing).toBe(false);
    }
    expect(start).not.toHaveBeenCalled();
  });

  it('keeps an unknown quote unknown and shows provider claims', async () => {
    const { result } = setup(vi.fn(async () => ({ ...quote('one', null), claims: [{ field: 'egress', display: 'Audio leaves this server.' }] })));
    const controller = new AbortController();
    let preparing!: Promise<boolean>;
    act(() => { preparing = result.current.prepareRun([{ doc, column: columns[0] }], controller.signal); });
    await waitFor(() => expect(result.current.gate).toBeTruthy());
    expect(result.current.gate!.props.estimate.billed_cost).toBeNull();
    expect(result.current.gate!.props.estimate.claims[0].display).toContain('Audio leaves this server.');
    act(() => controller.abort());
    await preparing;
  });

  it('keeps quote failures per candidate while allowing another candidate to finish', async () => {
    const { result, start } = setup(vi.fn(async (_file, input) => {
      if (input.engine === 'one') throw new Error('Engine unavailable');
      return { ...quote('two'), requires_confirmation: false };
    }));
    const signal = new AbortController().signal;
    expect(await result.current.prepareRun(columns.map((column) => ({ doc, column })), signal)).toBe(true);
    expect(await result.current.runColumn(doc, columns[0], undefined, signal)).toEqual({ ok: false, message: 'Engine unavailable' });
    expect(await result.current.runColumn(doc, columns[1], undefined, signal)).toMatchObject({ ok: true, results: 'transcript' });
    expect(start).toHaveBeenCalledTimes(1);
  });

  it('clears the displayed quote when comparison inputs change', async () => {
    const { result } = setup(vi.fn(async () => ({ ...quote('one'), requires_confirmation: false })));
    const signal = new AbortController().signal;
    await result.current.prepareRun([{ doc, column: columns[0] }], signal);
    await waitFor(() => expect(result.current.quote).toBeTruthy());
    act(() => result.current.clearQuote());
    expect(result.current.quote).toBeNull();
  });

  it('cancels a job whose start response arrives after cancellation', async () => {
    const { result, start, api } = setup(vi.fn(async () => ({ ...quote('one'), requires_confirmation: false })));
    const controller = new AbortController();
    await result.current.prepareRun([{ doc, column: columns[0] }], controller.signal);
    let release!: (value: { previewId: string; total: number }) => void;
    start.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    const running = result.current.runColumn(doc, columns[0], undefined, controller.signal);
    controller.abort();
    release({ previewId: 'late-preview', total: 1 });
    expect(await running).toMatchObject({ ok: false });
    expect(api.cancelPreview).toHaveBeenCalledWith('late-preview');
    expect(api.getPreview).not.toHaveBeenCalled();
  });
});
