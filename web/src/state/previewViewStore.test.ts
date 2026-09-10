// Unit tests of previewViewStore's own actions in isolation — no cross-store
// composition here. Cross-domain transaction-parity tests (the
// resetForRouteSheetChange composition site) live in
// state/workspaceTransitions.test.ts, not this file.

import { describe, expect, it, vi } from 'vitest';
import { createPreviewViewStore, previewViewForSheet } from './previewViewStore';
import type { PreviewGridView } from './previewViewStore';
import { createProjectApi } from '../api/real';

const api = createProjectApi('preview-view-test');

const preview = (sheetId: string, previewId = ''): PreviewGridView => ({
  sheetId,
  previewId,
  status: 'running',
  progress: { done: 0, total: 0 },
  result: null,
  actionName: 'Action',
  rowCount: 0,
  totalRows: 10,
  error: null,
  req: {
    action_id: 'map.classify',
    scope: { kind: 'sheet_rows', sheet_id: Number(sheetId.replace(/\D/g, '')) || 1 },
    params: {},
    output_names: { result: 'result' },
    idempotency_key: 'preview-view-test',
  },
});

describe('createPreviewViewStore — unit', () => {
  it.each(['done', 'error', 'cancelled'] as const)('retains paid accounting on %s', async (status) => {
    vi.useFakeTimers();
    try {
      const accounting = { receipt_id: 'receipt-paid', status: status === 'done' ? 'completed' :
        status === 'error' ? 'failed' : 'cancelled', model_call_count: 1, cost_actual: null, elapsed_ms: 3000 };
      const previews = createPreviewViewStore({
        startPreview: vi.fn().mockResolvedValue({ previewId: 'paid', total: 1 }),
        getPreview: vi.fn().mockResolvedValue({ status, accounting, progress: { done: 0, total: 1 },
          sampled: 0, total: 1, error: { message: 'Provider failed' } }),
        cancelPreview: vi.fn(),
      });
      await previews.openPreviewView(preview('sheet-1').req, { id: 'sheet-1', rowCount: 1 }, {
        invalidateProjectData: vi.fn(), requestCostConfirmation: vi.fn(),
      });
      await vi.advanceTimersByTimeAsync(750);
      expect(previews.store.get().previewView).toMatchObject({ status, accounting });
      previews.dispose();
    } finally { vi.useRealTimers(); }
  });
  it('opens a source-free table without a sheet and keeps the exact request for Run for real', async () => {
    vi.useFakeTimers();
    try {
      const request = { action_id: 'import.files', scope: { kind: 'project' as const },
        params: {}, output_names: {}, sheet_name: 'Files', idempotency_key: 'table-sample' };
      const result = { kind: 'table' as const, previewId: 'table', status: 'done' as const,
        progress: { done: 1, total: null }, columns: [], rows: [{ name: { value: 'File' } }],
        sampled: 1, total: null, error: null, warnings: [] };
      const startPreview = vi.fn().mockResolvedValue({ previewId: 'table', total: null });
      const getPreview = vi.fn().mockResolvedValue(result);
      const cancelPreview = vi.fn().mockResolvedValue(undefined);
      const previews = createPreviewViewStore({ startPreview, getPreview, cancelPreview });
      await previews.openPreviewView(request, null, {
        invalidateProjectData: vi.fn(), requestCostConfirmation: vi.fn(),
      });
      expect(previews.store.get().previewView).toMatchObject({ sheetId: null,
        progress: { done: 0, total: null }, totalRows: null, result: null });
      await vi.advanceTimersByTimeAsync(750);
      const view = previews.store.get().previewView;
      expect(view).toMatchObject({ sheetId: null, status: 'done', result, rowCount: 1, totalRows: null, req: request });
      expect(view?.result).not.toHaveProperty('sheetId');
      expect(view?.result).not.toHaveProperty('rowIds');
      expect(previewViewForSheet(view, undefined)).toBe(view);
      expect(startPreview).toHaveBeenCalledExactlyOnceWith(request);
      previews.dispose();
    } finally {
      vi.useRealTimers();
    }
  });
  it('starts with previewView null', () => {
    const { store } = createPreviewViewStore(api);
    expect(store.get().previewView).toBeNull();
  });

});

import { ConfirmationRequiredError } from '../api/open';

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('createPreviewViewStore — preview lifecycle additions', () => {
  it.each(['clearPreviewView', 'resetForSheetChange'] as const)(
    '%s clears an opened preview',
    async (clear) => {
      vi.useFakeTimers();
      try {
        vi.spyOn(api, 'startPreview').mockResolvedValue({ previewId: 'preview-1', total: 10 });
        vi.spyOn(api, 'cancelPreview').mockResolvedValue(undefined);
        const previews = createPreviewViewStore(api);

        await previews.openPreviewView(
          preview('sheet-1').req,
          { id: 'sheet-1', rowCount: 10 },
          { invalidateProjectData: vi.fn(), requestCostConfirmation: vi.fn() },
        );
        expect(previews.store.get().previewView?.previewId).toBe('preview-1');

        previews[clear]();
        expect(previews.store.get().previewView).toBeNull();
        previews.dispose();
      } finally {
        vi.useRealTimers();
        vi.restoreAllMocks();
      }
    },
  );

  it('does not let a stale start result overwrite the newer preview', async () => {
    vi.useFakeTimers();
    try {
      const staleStart = deferred<Awaited<ReturnType<typeof api.startPreview>>>();
      vi.spyOn(api, 'startPreview')
        .mockReturnValueOnce(staleStart.promise)
        .mockResolvedValueOnce({ previewId: 'preview-2', total: 8 });
      const cancelPreview = vi.spyOn(api, 'cancelPreview').mockResolvedValue(undefined);
      const previews = createPreviewViewStore(api);
      const deps = { invalidateProjectData: vi.fn(), requestCostConfirmation: vi.fn() };

      const first = previews.openPreviewView(
        preview('sheet-1').req,
        { id: 'sheet-1', rowCount: 10 },
        deps,
      );
      await previews.openPreviewView(
        preview('sheet-2').req,
        { id: 'sheet-2', rowCount: 8 },
        deps,
      );

      staleStart.resolve({ previewId: 'preview-1', total: 10 });
      await first;

      expect(previews.store.get().previewView).toMatchObject({
        sheetId: '2',
        previewId: 'preview-2',
        status: 'running',
      });
      expect(cancelPreview).toHaveBeenCalledWith('preview-1');
      previews.dispose();
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });

  it('does not let a stale poll result clear the newer preview', async () => {
    vi.useFakeTimers();
    try {
      const stalePoll = deferred<Awaited<ReturnType<typeof api.getPreview>>>();
      vi.spyOn(api, 'startPreview')
        .mockResolvedValueOnce({ previewId: 'preview-1', total: 10 })
        .mockResolvedValueOnce({ previewId: 'preview-2', total: 8 });
      const getPreview = vi.spyOn(api, 'getPreview').mockReturnValueOnce(stalePoll.promise);
      const cancelPreview = vi.spyOn(api, 'cancelPreview').mockResolvedValue(undefined);
      const invalidateProjectData = vi.fn();
      const previews = createPreviewViewStore(api);
      const deps = { invalidateProjectData, requestCostConfirmation: vi.fn() };

      await previews.openPreviewView(
        preview('sheet-1').req,
        { id: 'sheet-1', rowCount: 10 },
        deps,
      );
      vi.advanceTimersByTime(750);
      expect(getPreview).toHaveBeenCalledWith('preview-1');

      await previews.openPreviewView(
        preview('sheet-2').req,
        { id: 'sheet-2', rowCount: 8 },
        deps,
      );
      stalePoll.resolve({
        previewId: 'preview-1',
        status: 'cancelled',
        progress: { done: 0, total: 10 },
        kind: 'row_overlay' as const,
        sheetId: 'sheet-1',
        columns: [],
        rows: {},
        rowIds: [],
        sampled: 0,
        total: 10,
        error: null,
      });
      await Promise.resolve();
      await Promise.resolve();

      expect(previews.store.get().previewView).toMatchObject({
        sheetId: '2',
        previewId: 'preview-2',
        status: 'running',
      });
      expect(cancelPreview).toHaveBeenCalledWith('preview-1');
      expect(invalidateProjectData).not.toHaveBeenCalled();
      previews.dispose();
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });

  it('confirmation retry carries the exact server hash on the native request', async () => {
    vi.useFakeTimers();
    try {
      const estimate = {
        cost: 0.01,
        rows: 10,
        promise_set_hash: 'promise-set-1',
      } as never;
      const startPreview = vi
        .spyOn(api, 'startPreview')
        .mockRejectedValueOnce(new ConfirmationRequiredError(estimate, 'Confirm preview'))
        .mockResolvedValueOnce({ previewId: 'preview-1', total: 10 });
      vi.spyOn(api, 'cancelPreview').mockResolvedValue(undefined);
      const requestCostConfirmation = vi.fn().mockResolvedValue(true);
      const previews = createPreviewViewStore(api);

      await previews.openPreviewView(
        preview('sheet-1').req,
        { id: 'sheet-1', rowCount: 10 },
        { invalidateProjectData: vi.fn(), requestCostConfirmation },
      );

      expect(requestCostConfirmation).toHaveBeenCalledWith(estimate, 'Confirm preview');
      expect(startPreview).toHaveBeenNthCalledWith(
        2,
        expect.objectContaining({ confirmation: 'promise-set-1' }),
      );
      previews.dispose();
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });

  it('dispose during an in-flight poll stops polling and permits no further writes', async () => {
    vi.useFakeTimers();
    try {
      const inFlightPoll = deferred<Awaited<ReturnType<typeof api.getPreview>>>();
      vi.spyOn(api, 'startPreview').mockResolvedValue({ previewId: 'preview-1', total: 10 });
      const getPreview = vi.spyOn(api, 'getPreview').mockReturnValueOnce(inFlightPoll.promise);
      const cancelPreview = vi.spyOn(api, 'cancelPreview').mockResolvedValue(undefined);
      const invalidateProjectData = vi.fn();
      const previews = createPreviewViewStore(api);

      await previews.openPreviewView(
        preview('sheet-1').req,
        { id: 'sheet-1', rowCount: 10 },
        { invalidateProjectData, requestCostConfirmation: vi.fn() },
      );
      expect(vi.getTimerCount()).toBe(1);

      vi.advanceTimersByTime(750);
      expect(getPreview).toHaveBeenCalledTimes(1);

      const writeAfterDispose = vi.fn();
      const unsubscribe = previews.store.subscribe(writeAfterDispose);
      previews.dispose();
      const snapshotAfterDispose = previews.store.get();

      expect(cancelPreview).toHaveBeenCalledWith('preview-1');
      expect(vi.getTimerCount()).toBe(0);

      inFlightPoll.resolve({
        previewId: 'preview-1',
        status: 'done',
        progress: { done: 10, total: 10 },
        kind: 'row_overlay' as const,
        sheetId: 'sheet-1',
        columns: [],
        rows: {},
        rowIds: [1],
        sampled: 1,
        total: 10,
        error: null,
      });
      await Promise.resolve();
      await Promise.resolve();

      expect(previews.store.get()).toBe(snapshotAfterDispose);
      expect(writeAfterDispose).not.toHaveBeenCalled();
      expect(invalidateProjectData).not.toHaveBeenCalled();

      vi.advanceTimersByTime(1500);
      expect(getPreview).toHaveBeenCalledTimes(1);
      unsubscribe();
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });
});

describe('createPreviewViewStore — visibility pause addition', () => {
  it('V1 pauses preview polling while hidden and catches up exactly once on refocus', async () => {
    vi.useFakeTimers();
    const visibilityTarget = new EventTarget();
    Object.defineProperty(visibilityTarget, 'hidden', { configurable: true, value: false });
    vi.stubGlobal('document', visibilityTarget);
    try {
      vi.spyOn(api, 'startPreview').mockResolvedValue({ previewId: 'preview-1', total: 10 });
      const getPreview = vi.spyOn(api, 'getPreview').mockResolvedValue({
        previewId: 'preview-1',
        status: 'running',
        progress: { done: 1, total: 10 },
        kind: 'row_overlay' as const,
        sheetId: 'sheet-1',
        columns: [],
        rows: {},
        rowIds: [],
        sampled: 0,
        total: 10,
        error: null,
      });
      vi.spyOn(api, 'cancelPreview').mockResolvedValue(undefined);
      const previews = createPreviewViewStore(api);

      await previews.openPreviewView(
        preview('sheet-1').req,
        { id: 'sheet-1', rowCount: 10 },
        { invalidateProjectData: vi.fn(), requestCostConfirmation: vi.fn() },
      );
      await vi.advanceTimersByTimeAsync(750);
      expect(getPreview).toHaveBeenCalledTimes(1);

      Object.defineProperty(visibilityTarget, 'hidden', { configurable: true, value: true });
      visibilityTarget.dispatchEvent(new Event('visibilitychange'));
      await vi.advanceTimersByTimeAsync(2_250);
      expect(getPreview).toHaveBeenCalledTimes(1);

      Object.defineProperty(visibilityTarget, 'hidden', { configurable: true, value: false });
      visibilityTarget.dispatchEvent(new Event('visibilitychange'));
      await Promise.resolve();
      expect(getPreview).toHaveBeenCalledTimes(2);

      await vi.advanceTimersByTimeAsync(750);
      expect(getPreview).toHaveBeenCalledTimes(3);
      previews.dispose();
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
      vi.unstubAllGlobals();
    }
  });

  it('refocus during an in-flight preview poll keeps one call and one live timer', async () => {
    vi.useFakeTimers();
    const visibilityTarget = new EventTarget();
    Object.defineProperty(visibilityTarget, 'hidden', { configurable: true, value: false });
    vi.stubGlobal('document', visibilityTarget);
    try {
      const running: Awaited<ReturnType<typeof api.getPreview>> = {
        previewId: 'preview-1',
        status: 'running',
        progress: { done: 1, total: 10 },
        kind: 'row_overlay' as const,
        sheetId: 'sheet-1',
        columns: [],
        rows: {},
        rowIds: [],
        sampled: 0,
        total: 10,
        error: null,
      };
      vi.spyOn(api, 'startPreview').mockResolvedValue({ previewId: 'preview-1', total: 10 });
      const pendingPolls: Array<ReturnType<typeof deferred<Awaited<ReturnType<typeof api.getPreview>>>>> = [];
      let concurrentPolls = 0;
      let maxConcurrentPolls = 0;
      const getPreview = vi.spyOn(api, 'getPreview').mockImplementation(() => {
        concurrentPolls += 1;
        maxConcurrentPolls = Math.max(maxConcurrentPolls, concurrentPolls);
        const pending = deferred<Awaited<ReturnType<typeof api.getPreview>>>();
        pendingPolls.push(pending);
        return pending.promise.finally(() => {
          concurrentPolls -= 1;
        });
      });
      const cancelPreview = vi.spyOn(api, 'cancelPreview').mockResolvedValue(undefined);
      const previews = createPreviewViewStore(api);

      await previews.openPreviewView(
        preview('sheet-1').req,
        { id: 'sheet-1', rowCount: 10 },
        { invalidateProjectData: vi.fn(), requestCostConfirmation: vi.fn() },
      );
      vi.advanceTimersByTime(750);
      expect(getPreview).toHaveBeenCalledTimes(1);

      Object.defineProperty(visibilityTarget, 'hidden', { configurable: true, value: true });
      visibilityTarget.dispatchEvent(new Event('visibilitychange'));
      Object.defineProperty(visibilityTarget, 'hidden', { configurable: true, value: false });
      visibilityTarget.dispatchEvent(new Event('visibilitychange'));

      for (const pending of pendingPolls) pending.resolve(running);
      await vi.advanceTimersByTimeAsync(0);
      expect({
        maxConcurrentPolls,
        pollCalls: getPreview.mock.calls.length,
        liveTimers: vi.getTimerCount(),
      }).toEqual({ maxConcurrentPolls: 1, pollCalls: 1, liveTimers: 1 });

      Object.defineProperty(visibilityTarget, 'hidden', { configurable: true, value: true });
      visibilityTarget.dispatchEvent(new Event('visibilitychange'));
      previews.dispose();
      expect(cancelPreview).toHaveBeenCalledTimes(1);
      expect(cancelPreview).toHaveBeenCalledWith('preview-1');
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
      vi.unstubAllGlobals();
    }
  });
});

describe('createPreviewViewStore — poll contracts', () => {
  it('keeps preview advisory and does not change the registered run request', async () => {
    vi.useFakeTimers();
    try {
      const request = {
        action_id: 'resolve.fill_missing',
        scope: { kind: 'sheet_rows' as const, sheet_id: 7 },
        params: { source: 'agency', method: 'down' },
        output_names: { cleaned: 'agency_clean' },
        idempotency_key: 'preview-request-1',
      };
      const result = {
        previewId: 'preview-1',
        status: 'done' as const,
        progress: { done: 1, total: 1 },
        kind: 'row_overlay' as const,
        sheetId: '7',
        columns: [],
        rows: {},
        rowIds: [1],
        sampled: 1,
        total: 1,
        error: null,
      };
      vi.spyOn(api, 'startPreview').mockResolvedValue({ previewId: 'preview-1', total: 1 });
      vi.spyOn(api, 'getPreview').mockResolvedValue(result);
      vi.spyOn(api, 'cancelPreview').mockResolvedValue(undefined);
      const onComplete = vi.fn();
      const previews = createPreviewViewStore(api);

      await previews.openPreviewView(request, { id: '7', rowCount: 1 }, {
        invalidateProjectData: vi.fn(),
        requestCostConfirmation: vi.fn(),
        onComplete,
      });
      await vi.advanceTimersByTimeAsync(750);

      expect(onComplete).toHaveBeenCalledWith(result);
      expect(previews.store.get().previewView?.req).toEqual(request);
      previews.dispose();
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });

  it('keeps the exact three-failure budget before a terminal poll error', async () => {
    vi.useFakeTimers();
    try {
      vi.spyOn(api, 'startPreview').mockResolvedValue({ previewId: 'preview-1', total: 10 });
      const getPreview = vi.spyOn(api, 'getPreview').mockRejectedValue(new Error('offline'));
      vi.spyOn(api, 'cancelPreview').mockResolvedValue(undefined);
      const previews = createPreviewViewStore(api);

      await previews.openPreviewView(
        preview('sheet-1').req,
        { id: 'sheet-1', rowCount: 10 },
        { invalidateProjectData: vi.fn(), requestCostConfirmation: vi.fn() },
      );
      await vi.advanceTimersByTimeAsync(750);
      expect(getPreview).toHaveBeenCalledTimes(1);
      expect(previews.store.get().previewView?.status).toBe('running');
      await vi.advanceTimersByTimeAsync(750);
      expect(getPreview).toHaveBeenCalledTimes(2);
      expect(previews.store.get().previewView?.status).toBe('running');
      await vi.advanceTimersByTimeAsync(750);
      expect(getPreview).toHaveBeenCalledTimes(3);
      expect(previews.store.get().previewView).toMatchObject({
        status: 'error',
        error: 'offline',
      });
      expect(vi.getTimerCount()).toBe(0);
      previews.dispose();
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });

  it.each([
    ['cancelled', null],
    ['error', 'error'],
    ['done', 'done'],
  ] as const)('keeps terminal %s on the clear-vs-patch branch', async (status, expectedStatus) => {
    vi.useFakeTimers();
    try {
      vi.spyOn(api, 'startPreview').mockResolvedValue({ previewId: 'preview-1', total: 10 });
      vi.spyOn(api, 'getPreview').mockResolvedValue({
        previewId: 'preview-1',
        status,
        progress: { done: 10, total: 10 },
        kind: 'row_overlay' as const,
        sheetId: 'sheet-1',
        columns: [],
        rows: {},
        rowIds: [1],
        sampled: 1,
        total: 10,
        error: status === 'error' ? { code: 'preview_failed', message: 'failed' } : null,
      });
      const cancelPreview = vi.spyOn(api, 'cancelPreview').mockResolvedValue(undefined);
      const previews = createPreviewViewStore(api);

      await previews.openPreviewView(
        preview('sheet-1').req,
        { id: 'sheet-1', rowCount: 10 },
        { invalidateProjectData: vi.fn(), requestCostConfirmation: vi.fn() },
      );
      await vi.advanceTimersByTimeAsync(750);

      if (expectedStatus === null) expect(previews.store.get().previewView).toBeNull();
      else expect(previews.store.get().previewView?.status).toBe(expectedStatus);
      previews.dispose();
      expect(cancelPreview).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
      vi.restoreAllMocks();
    }
  });
});
