// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import type { GeneratedActionRequest, PreviewSampleResult } from '../../src/api/types';
import { createPreviewViewStore, previewViewForSheet } from '../../src/state/previewViewStore';

afterEach(() => vi.useRealTimers());
const req: GeneratedActionRequest = {
  action_id: 'map.classify', scope: { kind: 'sheet_rows', sheet_id: 7 },
  params: {}, output_names: { result: 'result' }, idempotency_key: 'preview-test',
};
const done = { kind: 'table', status: 'done', rows: [], sampled: 2, total: 8,
  progress: { done: 2, total: 2 } } as unknown as PreviewSampleResult;

function setup() {
  const api = {
    startPreview: vi.fn().mockResolvedValue({ previewId: 'preview-1', total: 2 }),
    getPreview: vi.fn().mockResolvedValue(done), cancelPreview: vi.fn().mockResolvedValue(undefined),
  };
  const handle = createPreviewViewStore(api);
  const deps = { invalidateProjectData: vi.fn(), requestCostConfirmation: vi.fn(), onComplete: vi.fn() };
  return { api, handle, deps };
}

it('keeps sheet projection, source-free scope and terminal result composition local', async () => {
  vi.useFakeTimers();
  const { api, handle, deps } = setup();
  await handle.openPreviewView(req, { id: '7', rowCount: 8 }, deps);
  expect(handle.store.get().previewView).toMatchObject({ sheetId: '7', totalRows: 8, status: 'running' });
  expect(previewViewForSheet(handle.store.get().previewView, '8')).toBeNull();
  await vi.advanceTimersByTimeAsync(750);
  expect(handle.store.get().previewView).toMatchObject({ status: 'done', rowCount: 2, result: done, req });
  expect(deps.onComplete).toHaveBeenCalledExactlyOnceWith(done);
  expect(deps.invalidateProjectData).toHaveBeenCalledOnce();
  const sourceFree = { ...req, scope: { kind: 'project' as const } };
  await handle.openPreviewView(sourceFree, null, deps);
  expect(handle.store.get().previewView).toMatchObject({ sheetId: null, totalRows: null });
  expect(previewViewForSheet(handle.store.get().previewView, '8')).not.toBeNull();
  handle.dispose();
  expect(api.cancelPreview).toHaveBeenCalledOnce();
});

it('ignores a replaced preview read and cancels a late start response', async () => {
  vi.useFakeTimers();
  const { api, handle, deps } = setup();
  let releaseRead!: (value: PreviewSampleResult) => void;
  api.getPreview.mockReturnValueOnce(new Promise((resolve) => { releaseRead = resolve; }));
  await handle.openPreviewView(req, { id: '7', rowCount: 8 }, deps);
  await vi.advanceTimersByTimeAsync(750);
  let releaseStart!: (value: { previewId: string; total: number }) => void;
  api.startPreview.mockReturnValueOnce(new Promise((resolve) => { releaseStart = resolve; }));
  const next = handle.openPreviewView(req, { id: '7', rowCount: 8 }, deps);
  releaseRead(done);
  await Promise.resolve();
  expect(handle.store.get().previewView?.status).toBe('running');
  expect(deps.onComplete).not.toHaveBeenCalled();
  handle.dispose();
  releaseStart({ previewId: 'late-preview', total: 2 });
  await next;
  expect(api.cancelPreview.mock.calls).toEqual([['preview-1'], ['late-preview']]);
});
