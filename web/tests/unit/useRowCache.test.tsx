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

  it('clears an old total on a key switch and rejects the old deferred response', async () => {
    const oldRequest = deferred<{ columns: []; total: number; rows: Row[] }>();
    const newRequest = deferred<{ columns: []; total: number; rows: Row[] }>();
    const getSheetData = vi.fn((
      _sheet: string,
      _offset: number,
      _limit: number,
      options?: SheetDataOptions | null,
    ) => options?.filter ? newRequest.promise : oldRequest.promise);
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
    expect(result.current.rowCount).toBe(999);

    await act(async () => oldRequest.resolve({ columns: [], total: 17, rows: [row(17)] }));
    expect(result.current.rowCount).toBe(999);
    expect(result.current.getRow(0)).toBeUndefined();

    await act(async () => newRequest.resolve({ columns: [], total: 3, rows: [row(3)] }));
    expect(result.current.rowCount).toBe(3);
    expect(result.current.getRow(0)?.id).toBe('3');
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
});
