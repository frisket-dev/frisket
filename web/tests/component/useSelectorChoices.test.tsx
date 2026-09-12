// @vitest-environment jsdom

import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type {
  HttpSelectorChoicesQuery,
  HttpSelectorChoicesResponse,
} from '../../src/generated/openHttpContracts';
import { useSelectorChoices, type SelectorChoicesLoader } from '../../src/engine-selector/useSelectorChoices';

const query = (model: string | null): HttpSelectorChoicesQuery => ({
  schema_version: 'frisket.selector_choices_query.v1',
  subject: { kind: 'copilot', ...(model === null ? {} : { model }) },
});

function response(choiceId: string): HttpSelectorChoicesResponse {
  return {
    schema_version: 'frisket.selector_choices.v1',
    project_id: 'project-a',
    subject: { kind: 'copilot' },
    depends_on: [],
    current_choice_id: choiceId,
    default_choice_id: choiceId,
    groups: [],
    orphaned_current: null,
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
      .mockResolvedValueOnce(response('copilot-choice'))
      .mockReturnValueOnce(nextSubject.promise);
    const actionQuery: HttpSelectorChoicesQuery = {
      schema_version: 'frisket.selector_choices_query.v1',
      subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} },
    };
    const { result, rerender } = renderHook(
      ({ request, queryKey }) => useSelectorChoices({
        projectId: 'project-a', query: request, queryKey, load,
      }),
      { initialProps: { request: query(null), queryKey: 'copilot' } },
    );

    await waitFor(() => expect(result.current.response?.current_choice_id).toBe('copilot-choice'));
    rerender({ request: actionQuery, queryKey: 'action' });

    expect(result.current.response).toBeNull();
    expect(result.current.loading).toBe(true);
    expect(result.current.stale).toBe(false);

    await act(async () => nextSubject.resolve(response('action-choice')));
    await waitFor(() => expect(result.current.response?.current_choice_id).toBe('action-choice'));
  });
});
