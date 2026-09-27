// @vitest-environment jsdom

import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type {
  HttpSelectorChoicesQuery,
  HttpSelectorChoicesResponse,
} from '../../src/generated/openHttpContracts';
import {
  useSelectorChoices,
  type SelectorChoicesLoader,
  type SelectorPullLoader,
} from '../../src/engine-selector/useSelectorChoices';
import type { ModelPullDto } from '../../src/api/types';

const query = (model: string | null): HttpSelectorChoicesQuery => ({
  schema_version: 'frisket.selector_choices_query.v1',
  subject: { kind: 'project_ask', ...(model === null ? {} : { model }) },
});

function response(choiceId: string): HttpSelectorChoicesResponse {
  return {
    schema_version: 'frisket.selector_choices.v1',
    project_id: 'project-a',
    subject: { kind: 'project_ask' },
    depends_on: [],
    current_choice_id: choiceId,
    default_choice_id: choiceId,
    groups: [],
    orphaned_current: null,
  } as HttpSelectorChoicesResponse;
}

function pull(overrides: Partial<ModelPullDto> = {}): ModelPullDto {
  return {
    schemaVersion: 'frisket.model_pull.v4',
    id: 7,
    display_name: 'Parakeet setup',
    operation_kind: 'engine_setup',
    capabilities: { cancel: true, retry: false, remove: false },
    endpoint_id: null,
    endpoint_origin: null,
    initiated_by: null,
    artifact: null,
    model: 'engine-setup:parakeet-tdt.local-onnx@1',
    status: 'running',
    phase: 'downloading',
    total_bytes: 1000,
    completed_bytes: 100,
    error: null,
    resolved_digest: null,
    resolved_size: null,
    created_at: '2026-09-12T00:00:00Z',
    started_at: '2026-09-12T00:00:01Z',
    finished_at: null,
    cancel_requested: false,
    ...overrides,
  };
}

function responseWithPull(modelPull: ModelPullDto | null): HttpSelectorChoicesResponse {
  const setup = modelPull === null ? null : {
    kind: 'engine_setup' as const,
    setup_ref: modelPull.model,
    scope: 'workspace' as const,
    can_mutate: true,
    can_start: false,
    blocked_by_operation: modelPull,
  };
  return {
    ...response('engine:parakeet'),
    groups: [{
      group_id: 'local', kind: 'local', label: 'Local', status: modelPull === null ? 'ready' : 'working',
      choices: [{
        choice_id: 'engine:parakeet', label: 'Parakeet', summary: '', description: '', facts: [],
        authored_selection: { kind: 'engine', engine: 'parakeet-tdt' },
        processing_destination: { kind: 'local', label: 'This computer' }, resolved_target: null,
        is_current: true, is_default: true, can_author: true, can_run: modelPull === null,
        status: modelPull === null ? 'ready' : 'working', blocker: null,
        active_operation: modelPull, model_card_url: null, setup,
      }],
    }],
  } as HttpSelectorChoicesResponse;
}

function deferred<Value>() {
  let resolve!: (value: Value) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<Value>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

describe('useSelectorChoices', () => {
  it('does not show a response from an aborted draft scope', async () => {
    const first = deferred<HttpSelectorChoicesResponse>();
    const second = deferred<HttpSelectorChoicesResponse>();
    const load = vi.fn<SelectorChoicesLoader>()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const { result, rerender } = renderHook(
      ({ queryKey, request }) => useSelectorChoices({
        projectId: 'project-a', query: request, queryKey, load,
      }),
      { initialProps: { queryKey: 'first', request: query('first') } },
    );

    await waitFor(() => expect(load).toHaveBeenCalledTimes(1));
    rerender({ queryKey: 'second', request: query('second') });
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    await act(async () => second.resolve(response('second-choice')));
    await waitFor(() => expect(result.current.response?.current_choice_id).toBe('second-choice'));

    await act(async () => first.resolve(response('first-choice')));
    expect(result.current.response?.current_choice_id).toBe('second-choice');
  });

  it('clears an error and reloads only when Retry asks it to', async () => {
    const load = vi.fn<SelectorChoicesLoader>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(response('recovered-choice'));
    const { result } = renderHook(() => useSelectorChoices({
      projectId: 'project-a', query: query(null), queryKey: 'initial', load,
    }));

    await waitFor(() => expect(result.current.error?.message).toBe('offline'));
    expect(load).toHaveBeenCalledTimes(1);
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.response?.current_choice_id).toBe('recovered-choice'));
    expect(result.current.error).toBeNull();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('keeps the current catalog mounted while a same-scope refresh is pending', async () => {
    const refreshed = deferred<HttpSelectorChoicesResponse>();
    const load = vi.fn<SelectorChoicesLoader>()
      .mockResolvedValueOnce(response('before-refresh'))
      .mockReturnValueOnce(refreshed.promise);
    const { result } = renderHook(() => useSelectorChoices({
      projectId: 'project-a', query: query(null), queryKey: 'initial', load,
    }));

    await waitFor(() => expect(result.current.response?.current_choice_id).toBe('before-refresh'));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.refreshing).toBe(true));
    expect(result.current.loading).toBe(false);
    expect(result.current.response?.current_choice_id).toBe('before-refresh');

    await act(async () => refreshed.resolve(response('after-refresh')));
    await waitFor(() => expect(result.current.response?.current_choice_id).toBe('after-refresh'));
  });

  it('retains a same-subject projection while an authored draft changes, but marks it stale', async () => {
    const changed = deferred<HttpSelectorChoicesResponse>();
    const load = vi.fn<SelectorChoicesLoader>()
      .mockResolvedValueOnce(response('before-selection'))
      .mockReturnValueOnce(changed.promise);
    const { result, rerender } = renderHook(
      ({ queryKey, request }) => useSelectorChoices({
        projectId: 'project-a', query: request, queryKey, load,
      }),
      { initialProps: { queryKey: 'before', request: query('before') } },
    );

    await waitFor(() => expect(result.current.response?.current_choice_id).toBe('before-selection'));
    rerender({ queryKey: 'after', request: query('after') });
    await waitFor(() => expect(result.current.stale).toBe(true));
    expect(result.current.response?.current_choice_id).toBe('before-selection');
    expect(result.current.loading).toBe(false);
    expect(result.current.refreshing).toBe(true);

    await act(async () => changed.resolve(response('after-selection')));
    await waitFor(() => expect(result.current.stale).toBe(false));
    expect(result.current.response?.current_choice_id).toBe('after-selection');
  });

  it('does not expose a previous project projection during the first render of a new scope', async () => {
    const nextProject = deferred<HttpSelectorChoicesResponse>();
    const load = vi.fn<SelectorChoicesLoader>()
      .mockResolvedValueOnce(response('project-a-choice'))
      .mockReturnValueOnce(nextProject.promise);
    const { result, rerender } = renderHook(
      ({ projectId }) => useSelectorChoices({
        projectId, query: query(null), queryKey: 'initial', load,
      }),
      { initialProps: { projectId: 'project-a' } },
    );

    await waitFor(() => expect(result.current.response?.current_choice_id).toBe('project-a-choice'));
    rerender({ projectId: 'project-b' });

    expect(result.current.response).toBeNull();
    expect(result.current.loading).toBe(true);
    expect(result.current.stale).toBe(false);

    await act(async () => nextProject.resolve(response('project-b-choice')));
    await waitFor(() => expect(result.current.response?.current_choice_id).toBe('project-b-choice'));
  });

  it('does not expose a previous subject projection during the first render of a new scope', async () => {
    const nextSubject = deferred<HttpSelectorChoicesResponse>();
    const load = vi.fn<SelectorChoicesLoader>()
      .mockResolvedValueOnce(response('ask-choice'))
      .mockReturnValueOnce(nextSubject.promise);
    const actionQuery: HttpSelectorChoicesQuery = {
      schema_version: 'frisket.selector_choices_query.v1',
      subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} },
    };
    const { result, rerender } = renderHook(
      ({ request, queryKey }) => useSelectorChoices({
        projectId: 'project-a', query: request, queryKey, load,
      }),
      { initialProps: { request: query(null), queryKey: 'ask' } },
    );

    await waitFor(() => expect(result.current.response?.current_choice_id).toBe('ask-choice'));
    rerender({ request: actionQuery, queryKey: 'action' });

    expect(result.current.response).toBeNull();
    expect(result.current.loading).toBe(true);
    expect(result.current.stale).toBe(false);

    await act(async () => nextSubject.resolve(response('action-choice')));
    await waitFor(() => expect(result.current.response?.current_choice_id).toBe('action-choice'));
  });

  it('polls only the active pull every two visible seconds and does not restart after a terminal refresh returns a stale active snapshot', async () => {
    vi.useFakeTimers();
    const active = pull();
    const done = pull({ status: 'done', completed_bytes: 1000, finished_at: '2026-09-12T00:00:04Z' });
    const load = vi.fn<SelectorChoicesLoader>()
      .mockResolvedValueOnce(responseWithPull(active))
      .mockResolvedValueOnce(responseWithPull(active));
    const loadPull = vi.fn<SelectorPullLoader>().mockResolvedValue(done);
    const { result } = renderHook(() => useSelectorChoices({
      projectId: 'project-a', query: query(null), queryKey: 'initial', load, loadPull,
    }));

    await act(async () => { await Promise.resolve(); });
    expect(result.current.response?.groups[0].choices[0].active_operation?.id).toBe(active.id);
    expect(load).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1999); });
    expect(loadPull).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });

    expect(loadPull).toHaveBeenCalledWith('workspace', active.id, { signal: expect.any(AbortSignal) });
    await act(async () => { await Promise.resolve(); });
    expect(load).toHaveBeenCalledTimes(2);
    expect(result.current.response?.groups[0].choices[0].active_operation?.status).toBe('running');
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(loadPull).toHaveBeenCalledTimes(1);
  });

  it('pauses the shared pull poll while hidden, catches up on focus, and backs off after errors', async () => {
    vi.useFakeTimers();
    const hidden = Object.getOwnPropertyDescriptor(document, 'hidden');
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    const active = pull();
    const load = vi.fn<SelectorChoicesLoader>().mockResolvedValue(responseWithPull(active));
    const loadPull = vi.fn<SelectorPullLoader>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(active);
    const { result, unmount } = renderHook(() => useSelectorChoices({
      projectId: 'project-a', query: query(null), queryKey: 'initial', load, loadPull,
    }));

    await act(async () => { await Promise.resolve(); });
    expect(result.current.response).not.toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(loadPull).not.toHaveBeenCalled();

    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(loadPull).toHaveBeenCalledTimes(1);
    await act(async () => { await Promise.resolve(); });
    expect(result.current.operationError?.message).toBe('offline');
    await act(async () => { await vi.advanceTimersByTimeAsync(3999); });
    expect(loadPull).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(loadPull).toHaveBeenCalledTimes(2);

    unmount();
    if (hidden) Object.defineProperty(document, 'hidden', hidden);
    else delete (document as { hidden?: boolean }).hidden;
  });

  it('stops the shared pull poll when the selector is disabled', async () => {
    vi.useFakeTimers();
    const active = pull();
    const load = vi.fn<SelectorChoicesLoader>().mockResolvedValue(responseWithPull(active));
    const loadPull = vi.fn<SelectorPullLoader>().mockResolvedValue(active);
    const { result, rerender } = renderHook(
      ({ enabled }) => useSelectorChoices({
        projectId: 'project-a', query: query(null), queryKey: 'initial', load, loadPull, enabled,
      }),
      { initialProps: { enabled: true } },
    );

    await act(async () => { await Promise.resolve(); });
    expect(result.current.response).not.toBeNull();
    rerender({ enabled: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(loadPull).not.toHaveBeenCalled();
  });

  it('continues polling after a manual catalog refresh invalidates an in-flight pull read', async () => {
    vi.useFakeTimers();
    const active = pull();
    const stalePull = deferred<ModelPullDto>();
    const load = vi.fn<SelectorChoicesLoader>()
      .mockResolvedValueOnce(responseWithPull(active))
      .mockResolvedValueOnce(responseWithPull(active));
    const loadPull = vi.fn<SelectorPullLoader>()
      .mockReturnValueOnce(stalePull.promise)
      .mockResolvedValue(active);
    const { result } = renderHook(() => useSelectorChoices({
      projectId: 'project-a', query: query(null), queryKey: 'initial', load, loadPull,
    }));

    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(loadPull).toHaveBeenCalledTimes(1);
    act(() => result.current.refresh());
    await act(async () => { await Promise.resolve(); });
    expect(load).toHaveBeenCalledTimes(2);
    await act(async () => stalePull.resolve(active));
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(loadPull).toHaveBeenCalledTimes(2);
  });
});
