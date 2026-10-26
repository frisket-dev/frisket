// @vitest-environment jsdom
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { HttpSelectorChoicesQuery, HttpSelectorChoicesResponse, SelectorChoice } from '../../src/api/selectorChoices';
import { ActionCompareVariantChoiceOwner } from '../../src/workspace/WorkspaceActionCompareView';

afterEach(cleanup);

function query(model: string): HttpSelectorChoicesQuery {
  return {
    schema_version: 'frisket.selector_choices_query.v1',
    subject: { kind: 'action', action_id: 'map.extract', field: 'engine', params: { engine: 'llm', model } },
  } as HttpSelectorChoicesQuery;
}

function response(model: string, label: string): HttpSelectorChoicesResponse {
  const choice = {
    choice_id: model,
    authored_selection: { kind: 'engine_model', engine: 'llm', model },
    label,
    summary: '', description: '', facts: [], resolved_target: null,
    processing_destination: { kind: 'unknown', label: 'Test target' },
    execution_route: null, status: 'ready', can_author: true, can_run: true,
    blocker: null, setup: null, configuration_actions: [], active_operation: null,
    model_card_url: null,
    is_current: true,
  } as SelectorChoice;
  return {
    schema_version: 'frisket.selector_choices.v2', project_id: 'project-a',
    subject: { kind: 'action', action_id: 'map.extract', field: 'engine' },
    depends_on: ['engine', 'model'], current_choice_id: model, default_choice_id: null,
    groups: [{ group_id: 'provider', kind: 'provider', label: 'Provider', status: 'ready', choices: [choice] }],
    orphaned_current: null, cloud_setup: null,
  } as HttpSelectorChoicesResponse;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => { resolve = settle; });
  return { promise, resolve };
}

describe('Action Compare selector presentation', () => {
  it('publishes canonical labels unchanged and falls back while a changed query is stale', async () => {
    const first = deferred<HttpSelectorChoicesResponse>();
    const second = deferred<HttpSelectorChoicesResponse>();
    const load = vi.fn((_projectId: string, request: HttpSelectorChoicesQuery) => (
      request.subject.kind === 'action' && request.subject.params?.model === 'provider/model-b'
        ? second.promise : first.promise
    ));
    const publish = vi.fn();
    const view = render(<ActionCompareVariantChoiceOwner projectId="project-a" variantId="variant-b"
      query={query('provider/model-a')} publish={publish} load={load} />);

    await waitFor(() => expect(publish).toHaveBeenLastCalledWith('variant-b', null));
    first.resolve(response('provider/model-a', 'Claude Sonnet 4.5'));
    await waitFor(() => expect(publish).toHaveBeenLastCalledWith('variant-b',
      expect.objectContaining({ label: 'Claude Sonnet 4.5' })));

    view.rerender(<ActionCompareVariantChoiceOwner projectId="project-a" variantId="variant-b"
      query={query('provider/model-b')} publish={publish} load={load} />);
    await waitFor(() => expect(publish).toHaveBeenLastCalledWith('variant-b', null));
    second.resolve(response('provider/model-b', 'Qwen 3.5 4B'));
    await waitFor(() => expect(publish).toHaveBeenLastCalledWith('variant-b',
      expect.objectContaining({ label: 'Qwen 3.5 4B' })));
  });
});
