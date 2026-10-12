// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
    schema_version: 'frisket.selector_choices.v2',
    project_id: 'project-a',
    subject: { kind: 'action', action_id: 'map.classify', field: 'model' },
    depends_on: ['mode'],
    current_choice_id: 'ready-model',
    default_choice_id: 'ready-model',
    orphaned_current: null,
    cloud_setup: null,
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
        execution_route: null,
        route_selection: null,
        setup: null,
        configuration_actions: [],
        active_operation: null,
        is_default: true,
        is_current: true,
      }],
    }],
  } as HttpSelectorChoicesResponse;
}

describe('SelectorField', () => {
  it('changes a model route through the generated project endpoint and refreshes the projection', async () => {
    const payload = response();
    const selected = payload.groups[0].choices[0];
    selected.label = 'Gemini Flash';
    selected.route_selection = {
      model_id: 'google/gemini-flash', selected: 'cloud', can_mutate: true,
      options: [
        { id: 'cloud', label: 'Frisket Cloud', status: 'ready', disabled_reason: null },
        { id: 'own', label: 'Your Gemini key', status: 'needs_setup', disabled_reason: null },
      ],
    };
    selected.execution_route = {
      kind: 'cloud_credits', label: 'Frisket Cloud', billed_to: 'Frisket Cloud (Riverton News)',
      sends_data_to: 'Google via Frisket Cloud', has_alternative_route: true,
    };
    const load = vi.fn().mockResolvedValue(payload);
    request.mockResolvedValue({ model_id: 'google/gemini-flash', route: 'own' });
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} } }}
      recentNamespace="model-route" load={load} onSelect={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: /Gemini Flash/ }));
    await userEvent.click(screen.getByTestId('model-route-own'));

    await waitFor(() => expect(request).toHaveBeenCalledWith(
      'tenant.set_project_model_route.patch',
      expect.objectContaining({
        pathParams: { pid: 'project-a' },
        body: { model_id: 'google/gemini-flash', route: 'own' },
        signal: expect.any(AbortSignal),
      }),
    ));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
  });

  it('keeps list availability and ordering stable when the selected route needs setup', async () => {
    const before = response();
    const selected = before.groups[0].choices[0];
    selected.label = 'Gemini Flash';
    selected.route_selection = {
      model_id: 'google/gemini-flash', selected: 'cloud', can_mutate: true,
      options: [
        { id: 'cloud', label: 'Frisket Cloud', status: 'ready', disabled_reason: null },
        { id: 'own', label: 'Your Gemini API key', status: 'needs_setup', disabled_reason: null },
      ],
    };
    selected.execution_route = {
      kind: 'cloud_credits', label: 'Frisket Cloud', billed_to: 'Frisket credits',
      sends_data_to: 'Google via Frisket Cloud', has_alternative_route: true,
    };
    before.groups[0].choices.push({
      ...structuredClone(selected),
      choice_id: 'other-model',
      label: 'Other model',
      authored_selection: { kind: 'model', model: 'other-model' },
      route_selection: null,
      execution_route: null,
      is_current: false,
      is_default: false,
    });
    const after = structuredClone(before);
    Object.assign(after.groups[0].choices[0], {
      status: 'needs_setup',
      can_run: false,
      execution_route: null,
      setup: {
        kind: 'api_key', provider: 'gemini', scopes: [{
          scope: 'project', configured: false, can_mutate: true, source: 'missing',
          environment_names: ['GEMINI_API_KEY'], settings_location: 'project_ai_providers', hint: null,
        }],
      },
    });
    after.groups[0].choices[0].route_selection!.selected = 'own';
    const load = vi.fn().mockResolvedValueOnce(before).mockResolvedValueOnce(after);
    request.mockResolvedValue({ model_id: 'google/gemini-flash', route: 'own' });
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} } }}
      recentNamespace="stable-route-availability" load={load} onSelect={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: /Gemini Flash/ }));
    const dialog = screen.getByTestId('engine-selector-dialog');
    const choiceIds = () => Array.from(dialog.querySelectorAll('[data-engine-selector-choice]'))
      .map((element) => element.getAttribute('data-choice-id'));
    const geminiRow = () => dialog.querySelector('[data-choice-id="ready-model"]')!;
    expect(choiceIds()).toEqual(['ready-model', 'other-model']);
    expect(geminiRow()).toHaveTextContent('Frisket Cloud · Your Gemini API key');
    expect(geminiRow().querySelector('.engine-selector__status--ready')).not.toBeNull();

    await userEvent.click(within(dialog).getByTestId('model-route-own'));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(within(dialog).getByTestId('model-route-own'))
      .toHaveAttribute('aria-pressed', 'true'));
    expect(choiceIds()).toEqual(['ready-model', 'other-model']);
    expect(geminiRow()).toHaveTextContent('Frisket Cloud · Your Gemini API key');
    expect(geminiRow().querySelector('.engine-selector__status--ready')).not.toBeNull();
  });

  it('aggregates authoritative per-route network policy in list status and ordering', async () => {
    const payload = response();
    const blocked = payload.groups[0].choices[0];
    blocked.choice_id = 'network-blocked';
    blocked.label = 'Network-blocked model';
    blocked.status = 'unavailable';
    blocked.can_run = false;
    blocked.blocker = {
      code: 'project_network_off', message: 'Network access is off for this project.', field: null,
    };
    blocked.route_selection = {
      model_id: 'google/gemini-flash', selected: 'cloud', can_mutate: true,
      options: [
        { id: 'cloud', label: 'Frisket Cloud', status: 'unavailable',
          disabled_reason: 'Network access is off for this project.' },
        { id: 'own', label: 'Your Gemini API key', status: 'unavailable',
          disabled_reason: 'Network access is off for this project.' },
      ],
    };
    payload.current_choice_id = blocked.choice_id;
    payload.default_choice_id = blocked.choice_id;
    payload.groups[0].choices.push({
      ...structuredClone(blocked),
      choice_id: 'models-server-ready',
      label: 'Models server model',
      route_selection: {
        model_id: 'hosted/model', selected: 'cloud', can_mutate: true,
        options: [
          { id: 'cloud', label: 'Frisket Cloud', status: 'unavailable',
            disabled_reason: 'Network access is off for this project.' },
          { id: 'gateway', label: 'Models server', status: 'ready', disabled_reason: null },
        ],
      },
      is_current: false,
      is_default: false,
    });
    payload.groups[0].choices.push({
      ...structuredClone(blocked),
      choice_id: 'ready-second',
      label: 'Ready second model',
      status: 'ready',
      can_run: true,
      blocker: null,
      route_selection: null,
      is_current: false,
      is_default: false,
    });
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} } }}
      recentNamespace="network-policy-availability"
      load={vi.fn().mockResolvedValue(payload)} onSelect={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: /Network-blocked model/ }));
    const dialog = screen.getByTestId('engine-selector-dialog');
    const rows = Array.from(dialog.querySelectorAll<HTMLElement>('[data-engine-selector-choice]'));
    expect(rows.map((row) => row.dataset.choiceId)).toEqual([
      'models-server-ready', 'ready-second', 'network-blocked',
    ]);
    const modelsServerRow = dialog.querySelector('[data-choice-id="models-server-ready"]')!;
    expect(modelsServerRow).toHaveTextContent('Models server');
    expect(modelsServerRow).not.toHaveTextContent('Frisket Cloud');
    expect(modelsServerRow.querySelector('.engine-selector__status--ready')).not.toBeNull();
    const blockedRow = dialog.querySelector('[data-choice-id="network-blocked"]')!;
    expect(blockedRow.querySelector('.engine-selector__status--unavailable')).not.toBeNull();
  });

  it('links a configured own-key route to project provider settings without an editor accordion', async () => {
    const payload = response();
    const selected = payload.groups[0].choices[0];
    selected.label = 'Gemini Flash';
    selected.execution_route = {
      kind: 'local_key', label: 'Your Gemini API key', billed_to: 'Your Gemini API key',
      credential_label: 'Project key', sends_data_to: 'Google',
      settings_url: '/p/project-a/settings/project/ai-providers', has_alternative_route: true,
    };
    selected.configuration_actions = [{
      kind: 'api_key', provider: 'gemini', scopes: [{
        scope: 'project', configured: true, can_mutate: true, source: 'project',
        environment_names: ['GEMINI_API_KEY'], settings_location: 'project_ai_providers', hint: null,
      }],
    }];
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.extract', field: 'model', params: {} } }}
      recentNamespace="configured-key-settings" load={vi.fn().mockResolvedValue(payload)}
      onSelect={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: /Gemini Flash/ }));
    const dialog = within(screen.getByTestId('engine-selector-dialog'));
    expect(dialog.getByRole('link', { name: 'Your Gemini API key →' })).toHaveAttribute(
      'href', '/p/project-a/settings/project/ai-providers',
    );
    expect(dialog.queryByText('Project key')).not.toBeInTheDocument();
    expect(dialog.queryByRole('button', { name: /Use your own/ })).not.toBeInTheDocument();
  });

  it('keeps the selector open until a route change can trigger its refresh', async () => {
    const payload = response();
    const selected = payload.groups[0].choices[0];
    selected.label = 'Gemini Flash';
    selected.route_selection = {
      model_id: 'google/gemini-flash', selected: 'cloud', can_mutate: true,
      options: [
        { id: 'cloud', label: 'Frisket Cloud', status: 'ready', disabled_reason: null },
        { id: 'own', label: 'Your Gemini key', status: 'ready', disabled_reason: null },
      ],
    };
    let finishRoute!: (value: unknown) => void;
    request.mockReturnValue(new Promise((resolve) => { finishRoute = resolve; }));
    let finishRefresh!: (value: HttpSelectorChoicesResponse) => void;
    const refreshed = new Promise<HttpSelectorChoicesResponse>((resolve) => { finishRefresh = resolve; });
    const load = vi.fn().mockResolvedValueOnce(payload).mockReturnValueOnce(refreshed);
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} } }}
      recentNamespace="route-close-lifetime" load={load} onSelect={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: /Gemini Flash/ }));
    await userEvent.click(screen.getByTestId('model-route-own'));
    const dialog = screen.getByTestId('engine-selector-dialog');
    const close = within(dialog).getByRole('button', { name: 'Close Model selector' });
    expect(close).toBeDisabled();
    fireEvent(dialog, new Event('cancel', { bubbles: false, cancelable: true }));
    expect(screen.getByTestId('engine-selector-dialog')).toBeVisible();

    await act(async () => finishRoute({ model_id: 'google/gemini-flash', route: 'own' }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    expect(close).toBeDisabled();
    await act(async () => finishRefresh(payload));
    await waitFor(() => expect(close).toBeEnabled());
    await userEvent.click(close);
    expect(screen.queryByTestId('engine-selector-dialog')).not.toBeInTheDocument();
  });

  it('withholds stale route controls after refresh failure and restores the authoritative route on Retry', async () => {
    const before = response();
    const selected = before.groups[0].choices[0];
    selected.label = 'Gemini Flash';
    selected.route_selection = {
      model_id: 'google/gemini-flash', selected: 'cloud', can_mutate: true,
      options: [
        { id: 'cloud', label: 'Frisket Cloud', status: 'ready', disabled_reason: null },
        { id: 'own', label: 'Your Gemini key', status: 'ready', disabled_reason: null },
      ],
    };
    const after = structuredClone(before);
    after.groups[0].choices[0].route_selection!.selected = 'own';
    let recover!: (value: HttpSelectorChoicesResponse) => void;
    const recovery = new Promise<HttpSelectorChoicesResponse>((resolve) => { recover = resolve; });
    const load = vi.fn().mockResolvedValueOnce(before).mockRejectedValueOnce(new Error('offline'))
      .mockReturnValueOnce(recovery);
    request.mockResolvedValue({ model_id: 'google/gemini-flash', route: 'own' });
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} } }}
      recentNamespace="route-refresh-recovery" load={load} onSelect={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: /Gemini Flash/ }));
    const dialog = screen.getByTestId('engine-selector-dialog');
    await userEvent.click(within(dialog).getByTestId('model-route-own'));
    await waitFor(() => expect(within(dialog).getByRole('alert')).toHaveTextContent(
      'Could not refresh choices. offline',
    ));
    expect(within(dialog).queryByTestId('model-route-control')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Close Model selector' })).toBeEnabled();

    await userEvent.click(within(dialog).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(3));
    expect(within(dialog).queryByTestId('model-route-control')).not.toBeInTheDocument();
    await act(async () => recover(after));
    await waitFor(() => expect(within(dialog).getByTestId('model-route-own')).toHaveAttribute(
      'aria-pressed', 'true',
    ));
  });

  it('shows setup only for the selected route', async () => {
    const payload = response();
    const selected = payload.groups[0].choices[0];
    selected.label = 'Gemini Flash';
    selected.status = 'needs_setup';
    selected.route_selection = {
      model_id: 'google/gemini-flash', selected: 'own', can_mutate: true,
      options: [
        { id: 'cloud', label: 'Frisket Cloud', status: 'needs_setup', disabled_reason: null },
        { id: 'own', label: 'Your Gemini key', status: 'needs_setup', disabled_reason: null },
      ],
    };
    selected.setup = { kind: 'api_key', provider: 'gemini', scopes: [{
      scope: 'project', configured: false, can_mutate: true, source: 'missing',
      environment_names: ['GEMINI_API_KEY'], settings_location: 'project_ai_providers', hint: null,
    }] };
    selected.configuration_actions = [{
      kind: 'engine_setup', scope: 'workspace',
      setup_ref: 'engine-setup:gemini-local@1', can_mutate: true, can_start: true,
      blocked_by_operation: null,
    }];
    payload.cloud_setup = {
      kind: 'cloud', state: 'connect', account_id: null, account_name: null,
      balance_url: null, add_credits_url: null, project_settings_url: null,
    };
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} } }}
      recentNamespace="selected-route-setup" load={vi.fn().mockResolvedValue(payload)} onSelect={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: /Gemini Flash/ }));
    expect(screen.getByRole('heading', { name: 'Add your Gemini key' })).toBeVisible();
    expect(screen.queryByText('Connect Frisket Cloud to see available models.')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Sign in with browser/ })).not.toBeInTheDocument();
    expect(screen.queryByText('Set up on this computer')).not.toBeInTheDocument();
  });

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
    expect(within(dialog).getByRole('link', { name: 'Models server setup guide' })).toBeInTheDocument();
  });

  it('shows readable token pricing and setup instructions only once', async () => {
    const payload = response();
    const message = 'Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN to enable Cloudflare Clef classification.';
    Object.assign(payload.groups[0].choices[0], {
      label: 'Cloudflare Clef', status: 'unavailable', can_author: false, can_run: false,
      facts: [{ kind: 'rate', label: 'Cloudflare Clef classification (per input token)', amount: 0.00000024, currency: 'USD', unit: 'input_token', source_url: null, updated: null }],
      blocker: { code: 'engine_unavailable', message, field: null },
      setup: { kind: 'instructions', title: 'Connect Cloudflare', steps: [message, 'Configure these credentials in the server environment.'], url: null },
    });
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1', subject: { kind: 'action', action_id: 'map.classify', field: 'engine', params: {} } }}
      recentNamespace="clef-setup" load={vi.fn().mockResolvedValue(payload)} onSelect={vi.fn()} />);
    const trigger = await screen.findByRole('button', { name: /Cloudflare Clef/ });
    expect(trigger).toHaveTextContent(message);
    await userEvent.click(trigger);
    const dialog = within(screen.getByTestId('engine-selector-dialog'));
    expect(dialog.getByText('$0.24 per million input tokens')).toBeInTheDocument();
    expect(dialog.getAllByText(message)).toHaveLength(1);
    expect(dialog.getByText('Connect Cloudflare')).toBeInTheDocument();
  });

  it('uses the specific Cloud remediation without repeating a generic blocker', async () => {
    const payload = response();
    const generic = 'Set up this model to run it.';
    Object.assign(payload.groups[0].choices[0], {
      label: 'GPT-5 mini', summary: 'Out of Frisket credits',
      status: 'needs_setup', can_run: false,
      blocker: { code: 'cloud_credits_required', message: generic, field: 'model' },
      setup: {
        kind: 'cloud', state: 'out_of_credits', account_id: 11,
        account_name: 'Riverton News', add_credits_url: 'https://app.frisket.dev/credits',
        balance_url: null, project_settings_url: '/settings',
      },
    });
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.classify', field: 'model', params: {} } }}
      recentNamespace="cloud-remediation" load={vi.fn().mockResolvedValue(payload)} onSelect={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: /GPT-5 mini/ }));
    const dialog = within(screen.getByTestId('engine-selector-dialog'));
    expect(dialog.queryByText(generic)).not.toBeInTheDocument();
    expect(dialog.getByText('Riverton News is out of Frisket credits')).toBeVisible();
  });

  it('shows the selected Cloud route consistently and refreshes after saving an optional own key', async () => {
    const payload = response();
    Object.assign(payload.groups[0].choices[0], {
      label: 'Gemini Flash', summary: 'Catalog model',
      execution_route: {
        kind: 'cloud_credits', label: 'Investigations · Frisket credits',
        billed_to: 'Investigations · Frisket credits', sends_data_to: 'Google via Frisket Cloud',
        account_id: 41, balance_label: '$4.50 balance', balance_url: 'https://cloud.test/accounts/41',
        settings_url: '/p/project-a/settings/project/ai-providers', has_alternative_route: false,
      },
      configuration_actions: [{ kind: 'api_key', provider: 'gemini', scopes: [{
        scope: 'project', can_mutate: true, configured: false, source: 'missing', hint: null,
        environment_names: ['GEMINI_API_KEY'], settings_location: 'project_ai_providers',
      }] }],
    });
    const load = vi.fn().mockResolvedValue(payload);
    request.mockResolvedValueOnce({ provider: 'gemini', ok: true, reachable: true, status: 200,
      detail: null, validation_token: 'signed-key-receipt' }).mockResolvedValueOnce({});
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.extract', field: 'model', params: {} } }}
      recentNamespace="ready-cloud-configuration" load={load} onSelect={vi.fn()} />);

    const trigger = await screen.findByRole('button', { name: /Gemini Flash/ });
    expect(trigger).toHaveTextContent('Investigations · Frisket credits');
    expect(trigger).not.toHaveTextContent('Catalog model');
    await userEvent.click(trigger);
    const dialog = within(screen.getByTestId('engine-selector-dialog'));
    expect(dialog.getByTestId('engine-selector-route')).toHaveTextContent('Google via Frisket Cloud');
    const expand = dialog.getByRole('button', { name: /Use your own Google API key/ });
    expect(dialog.queryByLabelText('API key')).not.toBeInTheDocument();
    await userEvent.click(expand);
    await userEvent.type(dialog.getByLabelText('API key'), 'candidate-key');
    await userEvent.click(dialog.getByRole('button', { name: 'Test' }));
    await waitFor(() => expect(dialog.getByRole('button', { name: 'Save' })).toBeEnabled());
    const readsBeforeSave = load.mock.calls.length;
    await userEvent.click(dialog.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(load.mock.calls.length).toBeGreaterThan(readsBeforeSave));
    expect(request).toHaveBeenNthCalledWith(2, 'tenant.set_project_provider_key.post',
      expect.objectContaining({ pathParams: { pid: 'project-a' }, body: {
        provider: 'gemini', key: 'candidate-key', validation_token: 'signed-key-receipt',
      } }));
  });

  it('keeps a collapsed configuration draft authoritative through hover, while explicit selection can replace it', async () => {
    const payload = response();
    const selected = payload.groups[0].choices[0];
    selected.configuration_actions = [{ kind: 'api_key', provider: 'gemini', scopes: [{
      scope: 'project', can_mutate: true, configured: false, source: 'missing', hint: null,
      environment_names: ['GEMINI_API_KEY'], settings_location: 'project_ai_providers',
    }] }];
    payload.groups[0].choices.push({
      ...selected, choice_id: 'other-model', label: 'Other model',
      authored_selection: { kind: 'model', model: 'other-model' },
      configuration_actions: [], is_current: false, is_default: false,
    });
    const onSelect = vi.fn();
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.extract', field: 'model', params: {} } }}
      recentNamespace="collapsed-draft-hover" load={vi.fn().mockResolvedValue(payload)}
      onSelect={onSelect} />);

    await userEvent.click(await screen.findByRole('button', { name: /Ready model/ }));
    const dialog = screen.getByTestId('engine-selector-dialog');
    const setupToggle = within(dialog).getByRole('button', { name: /Use your own Google API key/ });
    await userEvent.click(setupToggle);
    await userEvent.type(within(dialog).getByLabelText('API key'), 'draft-key');
    await userEvent.click(setupToggle);

    const other = dialog.querySelector<HTMLButtonElement>('[data-engine-selector-choice="other-model"]')!;
    fireEvent.mouseEnter(other);
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(within(dialog).getByRole('heading', { name: 'Ready model' })).toBeVisible();
    expect(within(dialog).getByLabelText('API key')).toHaveValue('draft-key');
    expect(within(dialog).getByLabelText('API key')).not.toBeVisible();

    await userEvent.click(other);
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ choice_id: 'other-model' }));
    expect(screen.queryByTestId('engine-selector-dialog')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('API key')).not.toBeInTheDocument();
  });

  it('keeps an orphaned current selection authoritative instead of borrowing the default route', async () => {
    const payload = response();
    payload.current_choice_id = 'saved:removed-model';
    payload.orphaned_current = {
      ...payload.groups[0].choices[0],
      choice_id: 'saved:removed-model', label: 'Removed saved model', summary: 'Saved selection',
      authored_selection: { kind: 'model', model: 'removed/model' },
      status: 'unavailable', can_author: false, can_run: false, is_current: true, is_default: false,
      blocker: { code: 'model_unavailable', message: 'This saved model is no longer available.', field: 'model' },
      execution_route: null, setup: null, configuration_actions: [],
    };
    const current = vi.fn();
    render(<SelectorField projectId="project-a" label="Model"
      query={{ schema_version: 'frisket.selector_choices_query.v1',
        subject: { kind: 'action', action_id: 'map.extract', field: 'model', params: {} } }}
      recentNamespace="orphan-current" load={vi.fn().mockResolvedValue(payload)}
      onSelect={vi.fn()} onCurrentChoiceChange={current} />);

    const trigger = await screen.findByRole('button', { name: /Removed saved model/ });
    expect(trigger).toHaveTextContent('This saved model is no longer available.');
    expect(trigger).not.toHaveTextContent('Ready model');
    await waitFor(() => expect(current).toHaveBeenLastCalledWith(
      expect.objectContaining({ choice_id: 'saved:removed-model', can_run: false }),
    ));
    await userEvent.click(trigger);
    expect(within(screen.getByTestId('engine-selector-dialog')).getAllByText('Saved selection')).toHaveLength(2);
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
    await new Promise((resolve) => requestAnimationFrame(resolve));
    const keyInput = within(dialog).getByLabelText('API key');
    await userEvent.click(keyInput);
    await userEvent.type(keyInput, 'replacement-key');
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
