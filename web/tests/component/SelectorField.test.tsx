// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import type { HttpSelectorChoicesResponse } from '../../src/api/selectorChoices';
import { SelectorField } from '../../src/engine-selector/SelectorField';
import { installDialogPolyfill } from '../support/domPolyfills';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('../../src/api/httpContract', () => ({ httpContract: request }));
beforeAll(installDialogPolyfill);
afterEach(() => { cleanup(); vi.resetAllMocks(); });

function response(): HttpSelectorChoicesResponse {
  return {
    schema_version: 'frisket.selector_choices.v1',
    project_id: 'project-a',
    subject: { kind: 'action', action_id: 'map.classify', field: 'model' },
    depends_on: ['mode'],
    current_choice_id: 'ready-model',
    default_choice_id: 'ready-model',
    orphaned_current: null,
    groups: [{
      group_id: 'local',
      label: 'Local',
      choices: [{
        choice_id: 'ready-model',
        label: 'Ready model',
        summary: '',
        description: '',
        model_card_url: null,
        authored_selection: { kind: 'model', model: 'ready-model' },
        resolved_target: null,
        processing_destination: { label: 'Local' },
        facts: [],
        status: 'ready',
        can_author: true,
        can_run: true,
        blocker: null,
        setup: null,
        active_operation: null,
        is_default: true,
        is_current: true,
      }],
    }],
  } as HttpSelectorChoicesResponse;
}

describe('SelectorField', () => {
  it('loads authoritative readiness while its trigger is disabled for an active run', async () => {
    const load = vi.fn().mockResolvedValue(response());
    const onCurrentChoiceChange = vi.fn();
    render(
      <SelectorField
        projectId="project-a"
        label="Model"
        query={{
          schema_version: 'frisket.selector_choices_query.v1',
          subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} },
        }}
        recentNamespace="project-a:action:map.classify:model"
        disabled
        load={load}
        onSelect={vi.fn()}
        onCurrentChoiceChange={onCurrentChoiceChange}
      />,
    );

    await waitFor(() => expect(onCurrentChoiceChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ choice_id: 'ready-model', can_run: true }),
    ));
    expect(load).toHaveBeenCalledWith('project-a', expect.any(Object), expect.any(Object));
    expect(screen.getByRole('button', { name: /ready model/i })).toBeDisabled();
  });

  it('replaces a raw missing gateway remediation with its actionable setup in the detail scroller', async () => {
    const missing = response();
    const choice = missing.groups[0].choices[0];
    Object.assign(choice, {
      label: 'Gateway model', status: 'needs_setup', can_run: false,
      blocker: { code: 'models_gateway_required', message: 'Set FRISKET_MODELS_URL and FRISKET_MODELS_TOKEN.', field: null },
      setup: { kind: 'models_gateway', scopes: [{
        scope: 'workspace', can_mutate: true, configured: false, source: 'missing',
        hint: null, environment_names: ['FRISKET_MODELS_URL', 'FRISKET_MODELS_TOKEN'], settings_location: null,
      }] },
    });
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} } }}
      recentNamespace="missing-gateway" load={vi.fn().mockResolvedValue(missing)} onSelect={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: /Gateway model/ }));
    const dialog = screen.getByTestId('engine-selector-dialog');
    const token = within(dialog).getByLabelText('Gateway token');
    expect(within(dialog).queryByText(/FRISKET_MODELS_URL/)).not.toBeInTheDocument();
    expect(dialog.querySelector('.engine-selector__detail-scroll')).toContainElement(token);
  });


  it('withholds a same-scope ready snapshot after failed refresh and throughout Retry until authoritative recovery', async () => {
    const ready = response();
    ready.groups[0].choices[0].setup = {
      kind: 'api_key', provider: 'openai', scopes: [{
        scope: 'project', can_mutate: true, configured: true, source: 'stored',
        hint: null, environment_names: [], settings_location: null,
      }],
    };
    let recover!: (value: HttpSelectorChoicesResponse) => void;
    const recovered = new Promise<HttpSelectorChoicesResponse>((resolve) => { recover = resolve; });
    const load = vi.fn().mockResolvedValueOnce(ready).mockRejectedValueOnce(new Error('offline'))
      .mockReturnValueOnce(recovered);
    request.mockResolvedValueOnce({ provider: 'openai', ok: true, reachable: true, status: 200,
      detail: null, validation_token: 'signed-key-receipt' }).mockResolvedValueOnce({});
    const current = vi.fn();
    const select = vi.fn();
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} } }}
      recentNamespace="same-scope-refresh" load={load} onSelect={select}
      onCurrentChoiceChange={current} />);
    await waitFor(() => expect(current).toHaveBeenLastCalledWith(expect.objectContaining({ can_run: true })));
    await userEvent.click(screen.getByRole('button', { name: /Ready model/ }));
    const dialog = screen.getByTestId('engine-selector-dialog');
    await userEvent.type(within(dialog).getByLabelText('API key'), 'replacement-key');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Test' }));
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Save' })).toBeEnabled());
    await userEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(within(dialog).getByRole('alert')).toHaveTextContent('Could not refresh choices. offline'));
    expect(load).toHaveBeenCalledTimes(2);
    expect(current).toHaveBeenLastCalledWith(null);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(3));
    expect(current).toHaveBeenLastCalledWith(null);
    expect(dialog).toBeVisible();
    await act(async () => { recover(response()); });
    await waitFor(() => expect(current).toHaveBeenLastCalledWith(expect.objectContaining({ can_run: true })));
    expect(dialog).toBeVisible();
    expect(select).not.toHaveBeenCalled();
  });

  it('keeps an open selector visible through a failed draft refresh and retries it', async () => {
    const load = vi.fn()
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce(response())
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(response());
    const onCurrentChoiceChange = vi.fn();
    const field = (mode: string) => <SelectorField
      projectId="project-a"
      label="Model"
      query={{
        schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: { mode } },
      }}
      recentNamespace="project-a:action:map.classify:model"
      load={load}
      onSelect={vi.fn()}
      onCurrentChoiceChange={onCurrentChoiceChange}
    />;
    const { rerender } = render(field('before'));

    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole('button', { name: /ready model/i })).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: /ready model/i }));
    expect(screen.getByTestId('engine-selector-dialog')).toBeVisible();

    rerender(field('after'));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(3));
    const dialog = screen.getByTestId('engine-selector-dialog');
    await waitFor(() => expect(within(dialog).getByRole('alert')).toHaveTextContent('Could not refresh choices. offline'));
    expect(dialog).toBeVisible();
    expect(onCurrentChoiceChange).toHaveBeenLastCalledWith(null);

    await userEvent.click(within(dialog).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(onCurrentChoiceChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ choice_id: 'ready-model', can_run: true }),
    ));
    expect(dialog).toBeVisible();
  });
});
