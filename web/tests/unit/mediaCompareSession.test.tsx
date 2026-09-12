// @vitest-environment jsdom

import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { EngineOption } from '../../src/api/open';
import { useMediaCompareSession, type MediaCompareConfig } from '../../src/workbench/mediaCompareSession';

vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({
    projectApi: { listActionCatalog: vi.fn().mockResolvedValue(null) },
    chromePreferences: { projectId: 'test' },
  }),
}));
vi.mock('../../src/components/useResizable', () => ({ useResizable: () => ({}) }));

const local = { id: 'local', label: 'Local', tier: 'local' } as EngineOption;
const paid = { id: 'paid', label: 'Paid', tier: 'hosted', billable: true } as EngineOption;

function config(overrides: Partial<MediaCompareConfig<string>> = {}): MediaCompareConfig<string> {
  return {
    testidPrefix: 'test', accept: '*', acceptHint: 'media',
    classifyFile: () => 'audio', defaultEngineIds: ['paid'], fallbackCatalog: [local, paid],
    enginesFromCatalog: () => [local, paid], docSecondary: () => '',
    runColumn: vi.fn().mockResolvedValue({ ok: true, results: 'text', confidence: null }),
    unitsForDoc: () => [], autoRun: false, noDiffUnitLabel: '', ...overrides,
  };
}

describe('useMediaCompareSession paid batches', () => {
  beforeEach(() => {
    vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:test'), revokeObjectURL: vi.fn() });
  });

  it('includes billable engines only when explicitly enabled', async () => {
    const { result } = renderHook(() => useMediaCompareSession(config({ includeBillableEngines: true }), vi.fn()));
    await waitFor(() => expect(result.current.catalog.map((engine) => engine.id)).toEqual(['local', 'paid']));
    expect(result.current.runnableColumns[0]?.engineId).toBe('paid');
  });

  it('does not arm a diff for a plain-output compare screen', async () => {
    const { result } = renderHook(() => useMediaCompareSession(config({
      defaultEngineIds: ['local', 'local'],
      fallbackCatalog: [local],
      enginesFromCatalog: () => [local],
      enableDiff: false,
    }), vi.fn()));
    await waitFor(() => expect(result.current.runnableColumns).toHaveLength(2));
    expect(result.current.effectiveMode).toBe('survey');
    expect(result.current.diffArmed).toBe(false);
  });

  it('prepares the exact batch once and starts nothing when consent is declined', async () => {
    const prepareRun = vi.fn().mockResolvedValue(false);
    const runColumn = vi.fn();
    const { result } = renderHook(() => useMediaCompareSession(config({ includeBillableEngines: true, prepareRun, runColumn }), vi.fn()));
    act(() => result.current.ingestFiles([new File(['x'], 'clip.wav')]));
    await waitFor(() => expect(result.current.pendingPairs).toHaveLength(1));
    const expected = result.current.pendingPairs;
    act(() => { result.current.runPending(); result.current.runPending(); });
    await waitFor(() => expect(prepareRun).toHaveBeenCalledTimes(1));
    expect(prepareRun.mock.calls[0][0]).toEqual(expected);
    await waitFor(() => expect(result.current.preparing).toBe(false));
    expect(runColumn).not.toHaveBeenCalled();
  });

  it('aborts preparation and exposes the same signal to runs', async () => {
    let release!: (approved: boolean) => void;
    const prepareRun = vi.fn((_pairs, signal: AbortSignal) => new Promise<boolean>((resolve) => {
      release = resolve;
      signal.addEventListener('abort', () => resolve(false));
    }));
    const runColumn = vi.fn();
    const { result } = renderHook(() => useMediaCompareSession(config({ includeBillableEngines: true, prepareRun, runColumn }), vi.fn()));
    act(() => result.current.ingestFiles([new File(['x'], 'clip.wav')]));
    await waitFor(() => expect(result.current.pendingPairs).toHaveLength(1));
    act(() => result.current.runPending());
    await waitFor(() => expect(result.current.preparing).toBe(true));
    const signal = prepareRun.mock.calls[0][1] as AbortSignal;
    act(() => result.current.cancelRuns());
    expect(signal.aborted).toBe(true);
    release(true);
    await waitFor(() => expect(result.current.preparing).toBe(false));
    expect(runColumn).not.toHaveBeenCalled();
  });

  it('runs sequential batches one at a time and cancellation skips pending variants', async () => {
    let finishFirst!: () => void;
    const runColumn = vi.fn((_doc, _column, _engine, signal: AbortSignal) =>
      new Promise<{ ok: true; results: string; confidence: null }>((resolve) => {
        finishFirst = () => resolve({ ok: true, results: 'text', confidence: null });
        signal.addEventListener('abort', finishFirst);
      }),
    );
    const { result } = renderHook(() => useMediaCompareSession(config({
      includeBillableEngines: true, sequential: true, runColumn,
    }), vi.fn()));
    act(() => result.current.ingestFiles([
      new File(['a'], 'one.wav'), new File(['b'], 'two.wav'),
    ]));
    await waitFor(() => expect(result.current.pendingPairs).toHaveLength(2));
    act(() => result.current.runPending());
    await waitFor(() => expect(runColumn).toHaveBeenCalledTimes(1));
    const signal = runColumn.mock.calls[0][3] as AbortSignal;
    act(() => result.current.cancelRuns());
    expect(signal.aborted).toBe(true);
    finishFirst();
    await waitFor(() => expect(result.current.runningCount).toBe(0));
    expect(runColumn).toHaveBeenCalledTimes(1);
  });

  it('does not let a cancelled batch completion change a replacement batch progress', async () => {
    let finishOld!: () => void;
    const runColumn = vi.fn()
      .mockImplementationOnce(() => new Promise((resolve) => {
        finishOld = () => resolve({ ok: true, results: 'old', confidence: null });
      }))
      .mockImplementationOnce(() => new Promise(() => {}));
    const { result } = renderHook(() => useMediaCompareSession(config({
      includeBillableEngines: true, runColumn,
    }), vi.fn()));
    act(() => result.current.ingestFiles([new File(['x'], 'clip.wav')]));
    await waitFor(() => expect(result.current.pendingPairs).toHaveLength(1));
    act(() => result.current.runPending());
    await waitFor(() => expect(runColumn).toHaveBeenCalledTimes(1));
    act(() => {
      result.current.cancelRuns();
      result.current.runAll();
    });
    await waitFor(() => expect(runColumn).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.runProgress).toEqual({ done: 0, total: 1 }));
    finishOld();
    await act(async () => { await Promise.resolve(); });
    expect(result.current.runProgress).toEqual({ done: 0, total: 1 });
  });
});
