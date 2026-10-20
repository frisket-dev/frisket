// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { PdfImportOcrSelection, PdfImportPreviewEstimate, PdfImportSnapshot, PdfImportsApi } from '../../src/api/pdfImports';
import { usePdfBatchPreview } from '../../src/components/importWorkspace/pdfBatch/usePdfBatchPreview';

afterEach(cleanup);

const selection: PdfImportOcrSelection = { kind: 'engine_model', engine: 'remote-ocr', model: 'vision-v1' };
const quote: PdfImportPreviewEstimate = {
  ocr_action_params: { engine: 'remote-ocr', model: 'vision-v1' }, cached: false,
  estimate: { cost: 0.1, rows: 1, requires_confirmation: true, promise_set_hash: 'quoted-token' },
};
const snapshot = (ocr = selection, draftId = 'draft-1') => ({
  draft_id: draftId, choices: { ocr_selection: ocr }, preview_jobs: [],
}) as unknown as PdfImportSnapshot;

function setup() {
  let resolveEstimate!: (estimate: PdfImportPreviewEstimate) => void;
  const estimate = vi.fn().mockReturnValue(new Promise((resolve) => { resolveEstimate = resolve; }));
  const start = vi.fn().mockResolvedValue({});
  const reportError = vi.fn();
  let currentDraft = 'draft-1';
  const api = { estimateOcrPreview: estimate, startOcrPreview: start } as unknown as PdfImportsApi;
  const view = renderHook(({ draft }) => usePdfBatchPreview({
    api, projectId: 'project-1', snapshot: draft, sampleFiles: [],
    currentDraft: () => currentDraft, reportError,
  }), { initialProps: { draft: snapshot() } });
  return { ...view, estimate, start, reportError, resolveEstimate,
    setDraft(id: string) { currentDraft = id; } };
}

it('executes only the quoted engine/model, draft, file, page and token', async () => {
  const view = setup();
  let pending!: Promise<void>;
  act(() => { pending = view.result.current.start('file-7', 3); });
  const requestSelection = view.estimate.mock.calls[0][4];
  expect(requestSelection).toEqual(selection);
  expect(requestSelection).not.toBe(selection);
  await act(async () => { view.resolveEstimate(quote); await pending; });
  const consent = view.result.current.consent!;
  expect(consent.intent).toMatchObject({
    projectId: 'project-1', draftId: 'draft-1', fileId: 'file-7', page: 3, selection,
  });
  const refresh = vi.fn().mockResolvedValue(undefined);
  await act(async () => { await view.result.current.confirm(consent, refresh); });
  expect(view.start).toHaveBeenCalledExactlyOnceWith('project-1', 'draft-1', 'file-7', 3,
    selection, 'quoted-token', { signal: expect.any(AbortSignal) });
  expect(refresh).toHaveBeenCalledOnce();
});

it.each(['selection', 'draft'] as const)('retires a deferred quote when its %s changes', async (change) => {
  const view = setup();
  let pending!: Promise<void>;
  act(() => { pending = view.result.current.start('file-7', 1); });
  const signal = view.estimate.mock.calls[0][5].signal as AbortSignal;
  if (change === 'draft') view.setDraft('draft-2');
  view.rerender({ draft: change === 'selection'
    ? snapshot({ ...selection, model: 'vision-v2' }) : snapshot(selection, 'draft-2') });
  expect(signal.aborted).toBe(true);
  expect(view.result.current.running).toBe(false);
  await act(async () => { view.resolveEstimate(quote); await pending; });
  expect(view.result.current.consent).toBeNull();
  expect(view.result.current.running).toBe(false);
  expect(view.start).not.toHaveBeenCalled();
  expect(view.reportError).not.toHaveBeenCalled();
});

it('cannot confirm an obsolete quote after engine change or cancellation', async () => {
  const view = setup();
  let pending!: Promise<void>;
  act(() => { pending = view.result.current.start('file-7', 1); });
  await act(async () => { view.resolveEstimate(quote); await pending; });
  const consent = view.result.current.consent!;
  view.rerender({ draft: snapshot({ kind: 'engine', engine: 'rapidocr' }) });
  expect(view.result.current.consent).toBeNull();
  await act(async () => { await view.result.current.confirm(consent); });
  expect(view.start).not.toHaveBeenCalled();
});

it('aborts an executing preview on unmount and ignores its late completion', async () => {
  const view = setup();
  let release!: () => void;
  view.start.mockReturnValueOnce(new Promise<void>((resolve) => { release = resolve; }));
  let pending!: Promise<void>;
  act(() => { pending = view.result.current.start('file-7', 1); });
  await act(async () => { view.resolveEstimate(quote); await pending; });
  const refresh = vi.fn();
  let executing!: Promise<void>;
  act(() => { executing = view.result.current.confirm(view.result.current.consent!, refresh); });
  const signal = view.start.mock.calls[0][6].signal as AbortSignal;
  view.unmount();
  expect(signal.aborted).toBe(true);
  await act(async () => { release(); await executing; });
  expect(refresh).not.toHaveBeenCalled();
});
