// @vitest-environment jsdom

import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { GridApiPort } from '../../src/api/ports';
import type { Row, SheetDataOptions } from '../../src/api/types';
import { createRowCacheStore } from '../../src/grid/rowCacheStore';
import { useRowCache } from '../../src/grid/useRowCache';

function row(id: number, index = id): Row {
  return { id: String(id), index, cells: {}, provenance: {} };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, reject, resolve };
}

function gridApi(overrides: {
  getSheetData?: GridApiPort['getSheetData'];
  getSheetRows?: NonNullable<GridApiPort['getSheetRows']>;
} = {}): GridApiPort {
  return {
    getSheetData: overrides.getSheetData ?? vi.fn(async () => ({
      columns: [], total: 1_500, rows: [row(0)],
    })),
    getSheetRows: overrides.getSheetRows ?? vi.fn(async (_sheet, offset) => ({
      rows: [row(offset, offset)],
    })),
  } as unknown as GridApiPort;
}

describe('useRowCache request epochs', () => {
  it('loads one full anchor initially, then rows-only pages while scrolling', async () => {
    const api = gridApi();
    const store = createRowCacheStore();
    const { result } = renderHook(() => useRowCache(
      '7', 1_500, 1, store, null, api,
    ));

    await waitFor(() => expect(api.getSheetData).toHaveBeenCalledTimes(1));
    expect(api.getSheetRows).not.toHaveBeenCalled();

    act(() => result.current.onVisibleRowsChanged(0, 500));
    await waitFor(() => expect(api.getSheetRows).toHaveBeenCalledTimes(1));
    expect(api.getSheetRows).toHaveBeenCalledWith('7', 500, 500, {});
  });

  it('reanchors the exact total when the inventory row count changes', async () => {
    const getSheetData = vi.fn()
      .mockResolvedValueOnce({ columns: [], total: 1_500, rows: [row(0)] })
      .mockResolvedValueOnce({ columns: [], total: 1_750, rows: [row(0)] });
    const api = gridApi({ getSheetData });
    const store = createRowCacheStore();
    const { result, rerender } = renderHook(
      ({ inventoryRows }: { inventoryRows: number }) =>
        useRowCache('7', inventoryRows, 1, store, null, api),
      { initialProps: { inventoryRows: 1_500 } },
    );
    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(1));
    expect(result.current.rowCount).toBe(1_500);

    rerender({ inventoryRows: 2_000 });

    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.rowCount).toBe(1_750));
  });

  it('uses one full anchor per refresh and rows-only loaded siblings', async () => {
    const api = gridApi();
    const store = createRowCacheStore();
    const { result } = renderHook(() => useRowCache('7', 1_500, 1, store, null, api));
    await waitFor(() => expect(api.getSheetData).toHaveBeenCalledTimes(1));

    act(() => result.current.onVisibleRowsChanged(0, 500));
    await waitFor(() => expect(api.getSheetRows).toHaveBeenCalledTimes(1));

    for (let tick = 1; tick <= 5; tick += 1) {
      act(() => result.current.refresh());
      await waitFor(() => expect(api.getSheetData).toHaveBeenCalledTimes(1 + tick));
      await waitFor(() => expect(api.getSheetRows).toHaveBeenCalledTimes(1 + tick));
    }
  });

  it('rejects stale responses across an A-to-B-to-A key cycle', async () => {
    const oldA = deferred<{ columns: []; total: number; rows: Row[] }>();
    const requestB = deferred<{ columns: []; total: number; rows: Row[] }>();
    const newA = deferred<{ columns: []; total: number; rows: Row[] }>();
    const getSheetData = vi.fn()
      .mockImplementationOnce(() => oldA.promise)
      .mockImplementationOnce(() => requestB.promise)
      .mockImplementationOnce(() => newA.promise);
    const api = gridApi({ getSheetData });
    const store = createRowCacheStore();
    const { result, rerender } = renderHook(
      ({ options }: { options: SheetDataOptions | null }) =>
        useRowCache('7', 999, 1, store, options, api),
      { initialProps: { options: null } },
    );
    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(1));

    rerender({ options: { filter: { score: { gte: '2' } } } });
    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(2));
    rerender({ options: null });
    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(3));
    expect(result.current.rowCount).toBe(999);

    await act(async () => oldA.resolve({ columns: [], total: 17, rows: [row(17)] }));
    expect(result.current.rowCount).toBe(999);
    expect(result.current.getRow(0)).toBeUndefined();

    await act(async () => requestB.resolve({ columns: [], total: 2, rows: [row(2)] }));
    expect(result.current.rowCount).toBe(999);
    await act(async () => newA.resolve({ columns: [], total: 3, rows: [row(3)] }));
    expect(result.current.rowCount).toBe(3);
    expect(result.current.getRow(0)?.id).toBe('3');
  });

  it('coalesces refreshes during an anchor into one trailing refresh', async () => {
    const pendingAnchor = deferred<{ columns: []; total: number; rows: Row[] }>();
    const getSheetData = vi.fn()
      .mockResolvedValueOnce({ columns: [], total: 10, rows: [row(0)] })
      .mockImplementationOnce(() => pendingAnchor.promise)
      .mockResolvedValueOnce({ columns: [], total: 12, rows: [row(0)] });
    const api = gridApi({ getSheetData });
    const store = createRowCacheStore();
    const { result } = renderHook(() => useRowCache('7', 10, 1, store, null, api));
    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(1));

    act(() => result.current.refresh());
    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(2));
    act(() => {
      result.current.refresh();
      result.current.refresh();
    });
    expect(getSheetData).toHaveBeenCalledTimes(2);

    await act(async () => pendingAnchor.resolve({ columns: [], total: 11, rows: [row(0)] }));
    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(3));
    expect(result.current.rowCount).toBe(12);
  });

  it('loads a page revealed while a refresh anchor is pending', async () => {
    const pendingAnchor = deferred<{ columns: []; total: number; rows: Row[] }>();
    const getSheetData = vi.fn()
      .mockResolvedValueOnce({ columns: [], total: 1_500, rows: [row(0)] })
      .mockImplementationOnce(() => pendingAnchor.promise);
    const getSheetRows = vi.fn(async (_sheet, offset) => ({
      rows: [row(offset, offset)],
    }));
    const api = gridApi({ getSheetData, getSheetRows });
    const store = createRowCacheStore();
    const { result } = renderHook(() => useRowCache('7', 1_500, 1, store, null, api));
    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(1));

    act(() => result.current.refresh());
    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(2));
    act(() => result.current.onVisibleRowsChanged(500, 500));
    expect(getSheetRows).not.toHaveBeenCalled();

    await act(async () => pendingAnchor.resolve({
      columns: [], total: 1_500, rows: [row(0)],
    }));
    await waitFor(() => expect(getSheetRows).toHaveBeenCalledTimes(1));
    expect(getSheetRows).toHaveBeenCalledWith('7', 500, 500, {});
    await waitFor(() => expect(result.current.getRow(500)?.id).toBe('500'));
    expect(getSheetData).toHaveBeenCalledTimes(2);
  });

  it('does not launch a coalesced refresh after unmount', async () => {
    const pendingAnchor = deferred<{ columns: []; total: number; rows: Row[] }>();
    const getSheetData = vi.fn()
      .mockResolvedValueOnce({ columns: [], total: 10, rows: [row(0)] })
      .mockImplementationOnce(() => pendingAnchor.promise);
    const api = gridApi({ getSheetData });
    const store = createRowCacheStore();
    const { result, unmount } = renderHook(() => useRowCache('7', 10, 1, store, null, api));
    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(1));

    act(() => result.current.refresh());
    await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(2));
    act(() => result.current.refresh());
    unmount();
    await act(async () => pendingAnchor.resolve({ columns: [], total: 11, rows: [row(0)] }));

    expect(getSheetData).toHaveBeenCalledTimes(2);
  });

  it('waits for refresh siblings before starting one coalesced trailing group', async () => {
    const pendingSibling = deferred<{ rows: Row[] }>();
    const getSheetRows = vi.fn()
      .mockResolvedValueOnce({ rows: [row(500, 500)] })
      .mockImplementationOnce(() => pendingSibling.promise)
      .mockResolvedValueOnce({ rows: [row(500, 500)] });
    const api = gridApi({ getSheetRows });
    const store = createRowCacheStore();
    const { result } = renderHook(() => useRowCache('7', 1_500, 1, store, null, api));
    await waitFor(() => expect(api.getSheetData).toHaveBeenCalledTimes(1));
    act(() => result.current.onVisibleRowsChanged(0, 500));
    await waitFor(() => expect(getSheetRows).toHaveBeenCalledTimes(1));

    act(() => result.current.refresh());
    await waitFor(() => expect(api.getSheetData).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getSheetRows).toHaveBeenCalledTimes(2));
    act(() => {
      result.current.refresh();
      result.current.refresh();
    });
    expect(api.getSheetData).toHaveBeenCalledTimes(2);
    expect(getSheetRows).toHaveBeenCalledTimes(2);

    await act(async () => pendingSibling.resolve({ rows: [row(500, 500)] }));
    await waitFor(() => expect(api.getSheetData).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(getSheetRows).toHaveBeenCalledTimes(3));
  });

  it('deduplicates an in-flight page and makes a rejected page retryable', async () => {
    const firstRows = deferred<{ rows: Row[] }>();
    const getSheetRows = vi.fn()
      .mockImplementationOnce(() => firstRows.promise)
      .mockResolvedValueOnce({ rows: [row(500, 500)] });
    const api = gridApi({ getSheetRows });
    const store = createRowCacheStore();
    const { result } = renderHook(() => useRowCache('7', 1_500, 1, store, null, api));
    await waitFor(() => expect(api.getSheetData).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.onVisibleRowsChanged(500, 500);
      result.current.onVisibleRowsChanged(500, 500);
    });
    await waitFor(() => expect(getSheetRows).toHaveBeenCalledTimes(1));
    await act(async () => firstRows.reject(new Error('temporary failure')));

    act(() => result.current.onVisibleRowsChanged(500, 500));
    await waitFor(() => expect(getSheetRows).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.getRow(500)?.id).toBe('500'));
  });

  it('retries a page whose loading sentinel belonged to an invalidated epoch', async () => {
    const staleRows = deferred<{ rows: Row[] }>();
    const getSheetRows = vi.fn()
      .mockResolvedValueOnce({ rows: [row(500, 500)] })
      .mockImplementationOnce(() => staleRows.promise)
      .mockResolvedValueOnce({ rows: [row(500, 500)] })
      .mockResolvedValueOnce({ rows: [row(1_000, 1_000)] });
    const api = gridApi({ getSheetRows });
    const store = createRowCacheStore();
    const { result } = renderHook(() => useRowCache('7', 1_500, 1, store, null, api));
    await waitFor(() => expect(api.getSheetData).toHaveBeenCalledTimes(1));

    act(() => result.current.onVisibleRowsChanged(500, 500));
    await waitFor(() => expect(getSheetRows).toHaveBeenCalledTimes(1));
    act(() => result.current.onVisibleRowsChanged(1_000, 1_000));
    await waitFor(() => expect(getSheetRows).toHaveBeenCalledTimes(2));
    act(() => {
      result.current.onVisibleRowsChanged(0, 0);
      result.current.refresh();
    });
    await waitFor(() => expect(api.getSheetData).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getSheetRows).toHaveBeenCalledTimes(3));

    await act(async () => staleRows.resolve({ rows: [row(1_000, 1_000)] }));
    expect(result.current.getRow(1_000)).toBeUndefined();
    act(() => result.current.onVisibleRowsChanged(1_000, 1_000));
    await waitFor(() => expect(getSheetRows).toHaveBeenCalledTimes(4));
    await waitFor(() => expect(result.current.getRow(1_000)?.id).toBe('1000'));
  });
});
