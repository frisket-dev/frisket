// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { SavedExtractionTemplate } from '../../src/api/documentExtraction';
import type { HttpExtractionTemplateSave } from '../../src/generated/openHttpContracts';
const api = vi.hoisted(() => ({ templates: vi.fn(), save: vi.fn(), select: vi.fn() }));
vi.mock('../../src/api/documentExtraction', () => ({ documentExtractionApi: api }));
import { EMPTY_EXTRACTION_DRAFT, useExtractionLayouts } from '../../src/workbench/extract/useExtractionLayouts';

let sequence = 0;
let projectId: string;
let serverLayout: SavedExtractionTemplate;
function saveDraft(pid: string, body: HttpExtractionTemplateSave) {
  serverLayout = { ...serverLayout, ...body, id: 1 };
  return structuredClone(serverLayout);
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
beforeEach(() => {
  projectId = `layout-persistence-${++sequence}`;
  serverLayout = { id: 1, name: 'Layout 1', sheet_id: 1, source: 'Document', source_column_id: 1,
    reference_row_id: null, repeat_group_id: null, has_applied: false, draft: structuredClone(EMPTY_EXTRACTION_DRAFT) };
  api.templates.mockImplementation(async () => ({ templates: [structuredClone(serverLayout)], selected_layout_id: 1 }));
  api.save.mockImplementation(saveDraft); api.select.mockResolvedValue(serverLayout);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it('flushes on leaving Extract and waits for that write before restoring the same source', async () => {
  const first = renderHook(() => useExtractionLayouts(projectId, '1', 'Document'));
  await waitFor(() => expect(first.result.current.loading).toBe(false));
  const pending = { tool: 'key' as const, region: { page: 1, box: { x0: .1, x1: .2, y0: .1, y1: .2 } } };
  act(() => first.result.current.change({ draft: { ...EMPTY_EXTRACTION_DRAFT, pending } }));
  const unsavedNavigation = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(unsavedNavigation);
  expect(unsavedNavigation.defaultPrevented).toBe(true);
  const delayed = deferred<SavedExtractionTemplate>(); api.save.mockImplementationOnce(() => delayed.promise);
  first.unmount();
  const second = renderHook(() => useExtractionLayouts(projectId, '1', 'Document'));
  await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1));
  expect(api.templates).toHaveBeenCalledTimes(1);
  await act(async () => { delayed.resolve(saveDraft(projectId, api.save.mock.lastCall![1])); });
  await waitFor(() => expect(second.result.current.loading).toBe(false));
  expect(second.result.current.layout?.draft.pending).toEqual(pending);
  expect(second.result.current.dirty).toBe(false);
  const savedNavigation = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(savedNavigation);
  expect(savedNavigation.defaultPrevented).toBe(false);
});

it('keeps a failed draft through a view switch and retries without replacing it with server state', async () => {
  const first = renderHook(() => useExtractionLayouts(projectId, '1', 'Document'));
  await waitFor(() => expect(first.result.current.loading).toBe(false));
  act(() => first.result.current.change({ draft: { ...EMPTY_EXTRACTION_DRAFT, continue_across_pages: true } }));
  api.save.mockRejectedValue(new Error('Disk full'));
  await act(async () => { await expect(first.result.current.flush()).rejects.toThrow('Disk full'); });
  expect(first.result.current.error).toBe('Disk full');
  first.unmount();
  const second = renderHook(() => useExtractionLayouts(projectId, '1', 'Document'));
  await waitFor(() => expect(second.result.current.loading).toBe(false));
  expect(second.result.current.layout?.draft.continue_across_pages).toBe(true);
  expect(second.result.current.dirty).toBe(true);
  api.save.mockImplementation(saveDraft);
  await act(async () => { await second.result.current.flush(); });
  expect(second.result.current.error).toBeNull(); expect(second.result.current.dirty).toBe(false);
  expect(serverLayout.draft.continue_across_pages).toBe(true);
});

it('serializes newer saves after a rejected earlier save and ignores that obsolete error', async () => {
  const hook = renderHook(() => useExtractionLayouts(projectId, '1', 'Document'));
  await waitFor(() => expect(hook.result.current.loading).toBe(false));
  const delayed = deferred<SavedExtractionTemplate>(); api.save.mockImplementationOnce(() => delayed.promise);
  act(() => hook.result.current.change({ draft: { ...EMPTY_EXTRACTION_DRAFT, continue_across_pages: true } }));
  let older!: Promise<SavedExtractionTemplate>;
  act(() => { older = hook.result.current.flush(); });
  const olderResult = older.catch((error: unknown) => error);
  await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1));
  act(() => hook.result.current.change({ draft: { ...EMPTY_EXTRACTION_DRAFT, continue_across_pages: true, expand_values: true } }));
  let newer!: Promise<SavedExtractionTemplate>;
  act(() => { newer = hook.result.current.flush(); });
  expect(api.save).toHaveBeenCalledTimes(1);
  await act(async () => { delayed.reject(new Error('Temporary write failure')); await olderResult; await newer; });
  expect(api.save).toHaveBeenCalledTimes(2);
  expect(serverLayout.draft.expand_values).toBe(true);
  expect(hook.result.current.layout?.draft.expand_values).toBe(true);
  expect(hook.result.current.error).toBeNull(); expect(hook.result.current.dirty).toBe(false);
});

it('autosaves a revert after the in-flight change succeeds instead of leaving the server on that change', async () => {
  const hook = renderHook(() => useExtractionLayouts(projectId, '1', 'Document'));
  await waitFor(() => expect(hook.result.current.loading).toBe(false));
  const delayed = deferred<SavedExtractionTemplate>(); api.save.mockImplementationOnce(() => delayed.promise);
  act(() => hook.result.current.change({ draft: { ...EMPTY_EXTRACTION_DRAFT, expand_values: true } }));
  let writing!: Promise<SavedExtractionTemplate>;
  act(() => { writing = hook.result.current.flush(); });
  await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1));
  act(() => hook.result.current.change({ draft: structuredClone(EMPTY_EXTRACTION_DRAFT) }));
  await act(async () => { delayed.resolve(saveDraft(projectId, api.save.mock.lastCall![1])); await writing; });
  await waitFor(() => expect(api.save).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(hook.result.current.dirty).toBe(false));
  expect(serverLayout.draft.expand_values).toBe(false);
  expect(hook.result.current.layout?.draft.expand_values).toBe(false);
});

it('does not coalesce an earlier A write when B and then A are queued behind it', async () => {
  const hook = renderHook(() => useExtractionLayouts(projectId, '1', 'Document'));
  await waitFor(() => expect(hook.result.current.loading).toBe(false));
  const delayed = deferred<SavedExtractionTemplate>(); api.save.mockImplementationOnce(() => delayed.promise);
  const draftA = { ...EMPTY_EXTRACTION_DRAFT, continue_across_pages: true };
  const draftB = { ...EMPTY_EXTRACTION_DRAFT, expand_values: true };
  act(() => hook.result.current.change({ draft: draftA }));
  let first!: Promise<SavedExtractionTemplate>;
  act(() => { first = hook.result.current.flush(); });
  await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1));
  act(() => hook.result.current.change({ draft: draftB }));
  let second!: Promise<SavedExtractionTemplate>;
  act(() => { second = hook.result.current.flush(); });
  act(() => hook.result.current.change({ draft: draftA }));
  let third!: Promise<SavedExtractionTemplate>;
  act(() => { third = hook.result.current.flush(); });
  expect(third).not.toBe(first);
  await act(async () => { delayed.resolve(saveDraft(projectId, api.save.mock.calls[0][1])); await Promise.all([first, second, third]); });
  expect(api.save.mock.calls.map((call) => call[1].draft)).toEqual([draftA, draftB, draftA]);
  expect(serverLayout.draft.continue_across_pages).toBe(true);
  expect(serverLayout.draft.expand_values).toBe(false);
  expect(hook.result.current.dirty).toBe(false);
});

it('flushes a revert on immediate unmount while a different draft is still being written', async () => {
  const hook = renderHook(() => useExtractionLayouts(projectId, '1', 'Document'));
  await waitFor(() => expect(hook.result.current.loading).toBe(false));
  const delayed = deferred<SavedExtractionTemplate>(); api.save.mockImplementationOnce(() => delayed.promise);
  act(() => hook.result.current.change({ draft: { ...EMPTY_EXTRACTION_DRAFT, expand_values: true } }));
  let writing!: Promise<SavedExtractionTemplate>;
  act(() => { writing = hook.result.current.flush(); });
  await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1));
  act(() => hook.result.current.change({ draft: structuredClone(EMPTY_EXTRACTION_DRAFT) }));
  hook.unmount();
  await act(async () => { delayed.resolve(saveDraft(projectId, api.save.mock.calls[0][1])); await writing; });
  await waitFor(() => expect(api.save).toHaveBeenCalledTimes(2));
  expect(serverLayout.draft.expand_values).toBe(false);
});

it('restores fresh applied metadata after a draft was changed and reverted without a write', async () => {
  const first = renderHook(() => useExtractionLayouts(projectId, '1', 'Document'));
  await waitFor(() => expect(first.result.current.loading).toBe(false));
  act(() => first.result.current.change({ draft: { ...EMPTY_EXTRACTION_DRAFT, expand_values: true } }));
  act(() => first.result.current.change({ draft: structuredClone(EMPTY_EXTRACTION_DRAFT) }));
  first.unmount();
  serverLayout.has_applied = true;
  const second = renderHook(() => useExtractionLayouts(projectId, '1', 'Document'));
  await waitFor(() => expect(second.result.current.loading).toBe(false));
  expect(second.result.current.layout?.has_applied).toBe(true);
  expect(second.result.current.layout?.draft.expand_values).toBe(false);
  expect(second.result.current.dirty).toBe(false);
});
