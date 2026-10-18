// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SelectorChoice } from '../../src/api/selectorChoices';
import { SelectorSetup } from '../../src/engine-selector/SelectorSetup';
import type { ModelPullDto } from '../../src/api/types';
import { ModelSetupRequestError } from '../../src/api/engineSetup';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('../../src/api/httpContract', () => ({ httpContract: request }));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks(); });

type Setup = NonNullable<SelectorChoice['setup']>;
type ConfigurationAction = SelectorChoice['configuration_actions'][number];
type Scope = Extract<Setup, { kind: 'api_key' }>['scopes'][number];
function scope(name: Scope['scope'], canMutate = true): Scope {
  return { scope: name, can_mutate: canMutate, configured: false,
    source: 'missing', hint: null, environment_names: [], settings_location: null };
}
function choice(setup: Setup | null, configurationActions: ConfigurationAction[] = []): SelectorChoice {
  return { choice_id: 'model:openai/test', label: 'Test model', summary: '', description: '',
    facts: [], authored_selection: { kind: 'model', model: 'openai/test' },
    processing_destination: { kind: 'external', label: 'Provider' }, resolved_target: null,
    is_current: false, is_default: false, can_author: true, can_run: false,
    status: setup ? 'needs_setup' : 'ready', blocker: null, active_operation: null,
    model_card_url: null, setup, configuration_actions: configurationActions };
}
const accepted = { provider: 'openai', ok: true, reachable: true, status: 200,
  detail: null, validation_token: 'signed-key-receipt' };
const gatewayAccepted = { schemaVersion: 'frisket.models_gateway_validation.v1',
  normalized_origin: 'https://models.test', validation_token: 'signed-gateway-receipt',
  probe: { ok: true, reachable: true, status: 200, detail: null,
    service: 'frisket-models', version: null, engines: [] } };

function deferred<Value>() {
  let resolve!: (value: Value) => void;
  const promise = new Promise<Value>((done) => { resolve = done; });
  return { promise, resolve };
}

function pull(overrides: Partial<ModelPullDto> = {}): ModelPullDto {
  return { schemaVersion: 'frisket.model_pull.v4', id: 7, operation_kind: 'engine_setup',
    display_name: 'Parakeet setup', model: 'engine-setup:parakeet-tdt.local-onnx@1',
    status: 'running', capabilities: { cancel: true, retry: false, remove: false },
    endpoint_id: null, endpoint_origin: null, cancel_requested: false, error: null,
    created_at: '2026-09-12T00:00:00Z', started_at: null, finished_at: null,
    initiated_by: null, phase: null, completed_bytes: null, total_bytes: null,
    resolved_digest: null, resolved_size: null, artifact: null, ...overrides };
}

describe('SelectorSetup', () => {
  it('shows Cloud first and reuses the validated project-key card as the local alternative', async () => {
    request.mockImplementation((operation: string) => {
      if (operation === 'tenant.get_cloud_connection.get') return Promise.resolve({
        state: 'disconnected', identity: null, accounts: [], expires_at: null,
        capabilities: { can_connect: true, can_reconnect: false, can_disconnect: false },
      });
      return Promise.reject(new Error(`Unexpected operation ${operation}`));
    });
    const selected = choice({
      kind: 'cloud', state: 'connect', account_id: null, account_name: null,
      add_credits_url: null, balance_url: null, project_settings_url: null,
    }, [{ kind: 'api_key', provider: 'gemini', scopes: [scope('project')] }]);
    render(<SelectorSetup projectId="a" choice={selected} cloudSetup={{
      kind: 'cloud', state: 'connect', account_id: null, account_name: null,
      add_credits_url: null, balance_url: null, project_settings_url: null,
    }} onChanged={vi.fn()} onEditingChange={vi.fn()} />);

    expect(await screen.findAllByRole('heading', { name: 'Connect Frisket Cloud' })).toHaveLength(1);
    expect(screen.queryByText('Connect Frisket Cloud to see available models.')).not.toBeInTheDocument();
    expect(screen.getByText('Each person gets $5 of free Frisket credit every month.')).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Use your own Google API key' })).toBeVisible();
    expect(screen.getByLabelText('API key')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument();
  });

  it('hard-stops an exhausted Cloud route and explicitly refreshes its account snapshot', async () => {
    request.mockResolvedValue({
      connection_state: 'connected', selected_account_id: 11, preference: 'cloud_first',
      preference_inherited: false, accounts: [], overlap_provider_labels: [], can_mutate: true,
    });
    const changed = vi.fn();
    render(<SelectorSetup projectId="a" choice={choice({
      kind: 'cloud', state: 'out_of_credits', account_id: 11, account_name: 'Riverton News',
      add_credits_url: 'https://app.frisket.dev/accounts/11/credits',
      balance_url: 'https://app.frisket.dev/accounts/11',
      project_settings_url: '/p/a/settings/project/ai-providers',
    })} onChanged={changed} onEditingChange={vi.fn()} />);

    expect(screen.getByText('Riverton News is out of Frisket credits')).toBeVisible();
    expect(screen.getByText(/won’t switch to another key or account/)).toBeVisible();
    expect(screen.getByRole('link', { name: 'Add credits ↗' })).toHaveAttribute(
      'href', 'https://app.frisket.dev/accounts/11/credits',
    );
    expect(screen.getByRole('link', { name: 'Project settings' })).toHaveAttribute(
      'href', '/p/a/settings/project/ai-providers',
    );
    await userEvent.click(screen.getByRole('button', { name: 'Recheck' }));
    await waitFor(() => expect(changed).toHaveBeenCalledOnce());
    expect(request).toHaveBeenCalledWith('tenant.get_project_cloud_routing.get', expect.objectContaining({
      pathParams: { pid: 'a' }, signal: expect.any(AbortSignal),
    }), expect.any(Function));
  });

  it('reconnects Cloud without replacing the project funding account', async () => {
    vi.useFakeTimers();
    const expired = {
      state: 'expired', identity: null, accounts: [], expires_at: '2026-10-10T12:00:00Z',
      capabilities: { can_connect: false, can_reconnect: true, can_disconnect: false },
    };
    const connected = {
      state: 'connected',
      identity: { id: 7, display_name: 'Sam', email: 'sam@example.test' },
      accounts: [{ id: 11, name: 'Riverton News', kind: 'organization', eligible: true,
        eligibility_reason: null, balance_label: '$10.00 balance', balance_url: null }],
      expires_at: '2027-01-11T00:00:00Z',
      capabilities: { can_connect: false, can_reconnect: false, can_disconnect: true },
    };
    request.mockImplementation((operation: string, _options: unknown, map?: (value: unknown) => unknown) => {
      const finish = (value: unknown) => Promise.resolve(map ? map(value) : value);
      if (operation === 'tenant.get_cloud_connection.get') return finish(expired);
      if (operation === 'tenant.begin_cloud_authorization.post') return finish({
        id: 'reconnect-1', user_code: 'ABCD-1234',
        verification_uri: 'https://app.frisket.dev/device', verification_uri_complete: null,
        expires_at: '2026-10-11T03:00:00Z', poll_interval_seconds: 1,
      });
      if (operation === 'tenant.poll_cloud_authorization.get') return finish({
        status: 'connected', connection: connected,
      });
      return Promise.reject(new Error(`Unexpected operation ${operation}`));
    });
    const changed = vi.fn();
    render(<SelectorSetup projectId="a" choice={choice({
      kind: 'cloud', state: 'reconnect', account_id: 11, account_name: 'Riverton News',
      add_credits_url: null, balance_url: null, project_settings_url: null,
    })} onChanged={changed} onEditingChange={vi.fn()} />);

    await act(async () => {});
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Reconnect' })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(request.mock.calls.map(([operation]) => operation)).not.toContain(
      'tenant.patch_project_cloud_routing.patch',
    );
    expect(changed).toHaveBeenCalledOnce();
  });

  it('validates then turns the compact project-key action into Save with its real receipt', async () => {
    request.mockResolvedValueOnce(accepted).mockResolvedValueOnce({});
    const changed = vi.fn();
    render(<SelectorSetup projectId="project-a" choice={choice({ kind: 'api_key', provider: 'openai', scopes: [scope('project')] })} onChanged={changed} onEditingChange={vi.fn()} />);
    expect(screen.getByRole('heading', { name: 'Add your OpenAI key' })).toBeVisible();
    expect(screen.getByText('How to get a key')).toBeVisible();
    expect(screen.getByRole('link', { name: /Open platform.openai.com/ })).toHaveAttribute(
      'href', 'https://platform.openai.com',
    );
    expect(screen.getByText('An API key works like a password.')).toBeVisible();
    expect(screen.queryByText(/Pay as you go/)).not.toBeInTheDocument();
    expect(screen.queryByText('Saved to this project.')).not.toBeInTheDocument();
    const key = screen.getByLabelText('API key');
    expect(key).toHaveAttribute('placeholder', 'sk-…');
    await userEvent.type(key, 'candidate-key{enter}');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    expect(screen.queryByRole('button', { name: 'Test' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(changed).toHaveBeenCalledOnce());
    expect(request).toHaveBeenNthCalledWith(1, 'tenant.validate_project_provider_key.post', expect.objectContaining({ pathParams: { pid: 'project-a' }, body: { provider: 'openai', key: 'candidate-key' }, signal: expect.any(AbortSignal) }));
    expect(request).toHaveBeenNthCalledWith(2, 'tenant.set_project_provider_key.post', expect.objectContaining({ pathParams: { pid: 'project-a' }, body: { provider: 'openai', key: 'candidate-key', validation_token: 'signed-key-receipt' } }));
    expect(screen.getByRole('button', { name: 'Test' })).toBeDisabled();
    expect(screen.getByLabelText('API key')).toHaveValue('');
  });

  it('keeps ready configuration collapsed until requested and preserves its draft across refreshes', async () => {
    const action = { kind: 'api_key' as const, provider: 'gemini', scopes: [scope('project')] };
    const changed = vi.fn(); const editing = vi.fn();
    const props = { projectId: 'project-a', onChanged: changed, onEditingChange: editing };
    const view = render(<SelectorSetup {...props} choice={choice(null, [action])} />);

    const expand = screen.getByRole('button', { name: /Use your own Google API key/ });
    expect(expand).toHaveAttribute('aria-expanded', 'false');
    const panelId = expand.getAttribute('aria-controls');
    expect(panelId).toBeTruthy();
    expect(document.getElementById(panelId!)).not.toBeVisible();
    expect(screen.queryByLabelText('API key')).not.toBeInTheDocument();
    await userEvent.click(expand);
    expect(expand).toHaveAttribute('aria-expanded', 'true');
    expect(document.getElementById(panelId!)).toBeVisible();
    expect(editing).toHaveBeenLastCalledWith(true);
    await userEvent.type(screen.getByLabelText('API key'), 'draft-key');

    const refreshed = choice(null, [{ ...action, scopes: [{ ...scope('project'), hint: 'updated' }] }]);
    view.rerender(<SelectorSetup {...props} choice={refreshed} />);
    expect(screen.getByLabelText('API key')).toHaveValue('draft-key');

    await userEvent.click(expand);
    expect(document.getElementById(panelId!)).not.toBeVisible();
    expect(screen.getByLabelText('API key')).toHaveValue('draft-key');
    await userEvent.click(expand);
    expect(screen.getByLabelText('API key')).toHaveValue('draft-key');
  });

  it('keeps an in-flight save mounted across collapse and retains its success feedback', async () => {
    const saved = deferred<Record<string, never>>();
    request.mockResolvedValueOnce(accepted).mockReturnValueOnce(saved.promise);
    const changed = vi.fn();
    render(<SelectorSetup projectId="project-a" choice={choice(null, [
      { kind: 'api_key', provider: 'openai', scopes: [scope('project')] },
    ])} onChanged={changed} onEditingChange={vi.fn()} />);

    const toggle = screen.getByRole('button', { name: /Use your own OpenAI API key/ });
    await userEvent.click(toggle);
    await userEvent.type(screen.getByLabelText('API key'), 'candidate-key');
    await userEvent.click(screen.getByRole('button', { name: 'Test' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    const saveSignal = request.mock.calls[1][1].signal as AbortSignal;

    await userEvent.click(toggle);
    expect(saveSignal.aborted).toBe(false);
    expect(screen.getByLabelText('API key')).not.toBeVisible();
    await act(async () => saved.resolve({}));
    await waitFor(() => expect(changed).toHaveBeenCalledOnce());
    await userEvent.click(toggle);
    expect(screen.getByText('Provider key saved')).toBeVisible();
  });

  it('does not render a configuration accordion for a ready own-key route', () => {
    const selected = choice(null, [
      { kind: 'api_key', provider: 'openai', scopes: [scope('project')] },
    ]);
    selected.execution_route = {
      kind: 'local_key', label: 'Your OpenAI API key', has_alternative_route: true,
    };
    render(<SelectorSetup projectId="project-a" choice={selected}
      onChanged={vi.fn()} onEditingChange={vi.fn()} />);

    expect(screen.queryByRole('button', { name: 'Change API key' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Use your own/ })).not.toBeInTheDocument();
  });

  it('keeps the selector editing lock while another configuration panel remains open', async () => {
    const editing = vi.fn();
    const view = render(<SelectorSetup projectId="project-a" choice={choice(null, [
      { kind: 'api_key', provider: 'gemini', scopes: [scope('project')] },
      { kind: 'models_gateway', scopes: [scope('organization')] },
    ])} onChanged={vi.fn()} onEditingChange={editing} />);

    const keyAction = screen.getByRole('button', { name: /Use your own Google API key/ });
    const gatewayAction = screen.getByRole('button', { name: 'Configure models server' });
    await userEvent.click(keyAction);
    expect(editing).toHaveBeenCalledTimes(1);
    expect(editing).toHaveBeenLastCalledWith(true);
    await userEvent.click(gatewayAction);
    expect(editing).toHaveBeenCalledTimes(1);

    await userEvent.click(keyAction);
    expect(keyAction).toHaveAttribute('aria-expanded', 'false');
    expect(gatewayAction).toHaveAttribute('aria-expanded', 'true');
    expect(editing).toHaveBeenCalledTimes(1);

    view.unmount();
    expect(editing).toHaveBeenNthCalledWith(2, false);
  });

  it('shows selector-wide Cloud discovery without claiming support for the inspected model', async () => {
    request.mockImplementation((operation: string) => {
      if (operation === 'tenant.get_cloud_connection.get') return Promise.resolve({
        state: 'disconnected', identity: null, accounts: [], expires_at: null,
        capabilities: { can_connect: true, can_reconnect: false, can_disconnect: false },
      });
      return Promise.reject(new Error(`Unexpected operation ${operation}`));
    });
    render(<SelectorSetup projectId="a" choice={choice(null)} cloudSetup={{
      kind: 'cloud', state: 'connect', account_id: null, account_name: null,
      add_credits_url: null, balance_url: null, project_settings_url: null,
    }} onChanged={vi.fn()} onEditingChange={vi.fn()} />);

    expect(screen.getByText('Connect Frisket Cloud to see available models.')).toBeVisible();
    expect(await screen.findByRole('heading', { name: 'Connect Frisket Cloud' })).toBeVisible();
  });

  it('expands a ready models-server action through its stable shared affordance', async () => {
    const environment = { ...scope('environment', false), source: 'environment' as const,
      configured: true, environment_names: ['FRISKET_MODELS_URL', 'FRISKET_MODELS_TOKEN'] };
    render(<SelectorSetup projectId="project-a" choice={choice(null, [{
      kind: 'models_gateway', scopes: [environment, scope('organization')],
    }])} onChanged={vi.fn()} onEditingChange={vi.fn()} />);

    expect(screen.queryByLabelText('Gateway URL')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Configure models server/ }));
    expect(screen.getByLabelText('Gateway URL')).toBeVisible();
    expect(screen.getByLabelText('Gateway token')).toBeVisible();
  });

  it('uses an authorized gateway scope without showing diagnostics or a scope picker', async () => {
    request.mockResolvedValue(gatewayAccepted);
    const environment = { ...scope('environment', false), source: 'environment' as const,
      configured: true, environment_names: ['FRISKET_MODELS_URL', 'FRISKET_MODELS_TOKEN'] };
    render(<SelectorSetup projectId="project-a" choice={choice({ kind: 'models_gateway', scopes: [environment, scope('organization')] })} onChanged={vi.fn()} onEditingChange={vi.fn()} />);
    expect(screen.queryByText(/FRISKET_MODELS_URL/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Credential scope')).not.toBeInTheDocument();
    const origin = screen.getByLabelText('Gateway URL');
    const token = screen.getByLabelText('Gateway token');
    const form = origin.closest('form');
    expect(form).toHaveClass('selector-setup-form--compact');
    expect(Array.from(form?.children ?? [])).toEqual([
      screen.getByRole('link', { name: 'Models server setup guide' }).closest('p'),
      origin.closest('label'),
      token.closest('label'),
      screen.getByRole('button', { name: 'Test' }),
    ]);
    await userEvent.type(screen.getByLabelText('Gateway URL'), 'https://models.test/');
    await userEvent.type(screen.getByLabelText('Gateway token'), 'gateway-token');
    await userEvent.click(screen.getByRole('button', { name: 'Test' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    expect(screen.queryByRole('button', { name: 'Test' })).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('Gateway token'), '-changed');
    expect(screen.getByRole('button', { name: 'Test' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Test' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(request).toHaveBeenCalledWith('outer.set_org_models_gateway.put', expect.objectContaining({ body: { origin: 'https://models.test', token: 'gateway-token-changed', validation_token: 'signed-gateway-receipt' } })));
    expect(request).toHaveBeenCalledWith('outer.validate_org_models_gateway.post', expect.objectContaining({ body: { origin: 'https://models.test/', token: 'gateway-token' } }));
  });

  it('does not expose environment diagnostics when no gateway scope is authorized', () => {
    const environment = { ...scope('environment', false), source: 'environment' as const,
      configured: true, environment_names: ['FRISKET_MODELS_URL', 'FRISKET_MODELS_TOKEN'] };
    render(<SelectorSetup projectId="a" choice={choice({ kind: 'models_gateway', scopes: [environment, scope('organization', false)] })} onChanged={vi.fn()} onEditingChange={vi.fn()} />);
    expect(screen.queryByText(/FRISKET_MODELS_URL/)).not.toBeInTheDocument();
    expect(screen.getByText('You do not have permission to complete this setup.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Gateway token')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });

  it('aborts and discards an old project validation, clearing secrets and editing state', async () => {
    let resolve: (value: typeof accepted) => void = () => {};
    request.mockImplementation(() => new Promise((done) => { resolve = done; }));
    const changed = vi.fn(); const editing = vi.fn();
    const selected = choice({ kind: 'api_key', provider: 'openai', scopes: [scope('project')] });
    const view = render(<SelectorSetup projectId="a" choice={selected} onChanged={changed} onEditingChange={editing} />);
    await userEvent.type(screen.getByLabelText('API key'), 'old-secret');
    await userEvent.click(screen.getByRole('button', { name: 'Test' }));
    const signal = request.mock.calls[0][1].signal;
    view.rerender(<SelectorSetup projectId="b" choice={selected} onChanged={changed} onEditingChange={editing} />);
    expect(signal.aborted).toBe(true);
    expect(screen.getByLabelText('API key')).toHaveValue('');
    await act(async () => resolve(accepted));
    expect(screen.getByRole('button', { name: 'Test' })).toBeDisabled();
    expect(changed).not.toHaveBeenCalled();
    expect(editing).toHaveBeenLastCalledWith(false);
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'new-key' } });
  });

  it('never falls back to a mutable workspace or organization key scope', () => {
    render(<SelectorSetup projectId="a" choice={choice({ kind: 'api_key', provider: 'openai', scopes: [scope('project', false), scope('workspace'), scope('organization')] })} onChanged={vi.fn()} onEditingChange={vi.fn()} />);
    expect(screen.queryByLabelText('API key')).not.toBeInTheDocument();
    expect(screen.getByText('You do not have permission to complete this setup.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Credential scope')).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
  });

  it('does not unlock save for a rejected key or a success without a receipt', async () => {
    request.mockResolvedValueOnce({ ...accepted, ok: false, validation_token: null, detail: 'bad candidate-key' })
      .mockResolvedValueOnce({ ...accepted, validation_token: null });
    render(<SelectorSetup projectId="a" choice={choice({ kind: 'api_key', provider: 'openai', scopes: [scope('project')] })} onChanged={vi.fn()} onEditingChange={vi.fn()} />);
    await userEvent.type(screen.getByLabelText('API key'), 'candidate-key');
    await userEvent.click(screen.getByRole('button', { name: 'Test' }));
    expect(await screen.findByRole('alert')).not.toHaveTextContent('candidate-key');
    expect(screen.getByRole('button', { name: 'Test' })).toBeEnabled();
    await userEvent.click(screen.getByRole('button', { name: 'Test' }));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('button', { name: 'Test' })).toBeEnabled();
  });

  it('keeps keyboard focus while shared download progress advances', () => {
    const initial = pull({ completed_bytes: 100, total_bytes: 1000 });
    const setup = { kind: 'engine_setup' as const, scope: 'workspace' as const,
      setup_ref: initial.model, can_mutate: true, can_start: false, blocked_by_operation: initial };
    const props = { projectId: 'a', onChanged: vi.fn(), onEditingChange: vi.fn(), sharedProgress: true };
    const view = render(<SelectorSetup {...props} choice={choice(setup)} />);
    screen.getByRole('button', { name: 'Recheck' }).focus();
    view.rerender(<SelectorSetup {...props} choice={choice({ ...setup,
      blocked_by_operation: pull({ completed_bytes: 600, total_bytes: 1000 }) })} />);
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '600');
    expect(screen.getByRole('progressbar')).toHaveAttribute('max', '1000');
    expect(screen.getByRole('button', { name: 'Recheck' })).toHaveFocus();
    expect(request).not.toHaveBeenCalled();
  });

  it('shows a busy durable operation, polling and requesting cancellation on its actual organization scope', async () => {
    const busy = pull({ model: 'hf:another-model@revision', display_name: 'Another model' });
    request.mockResolvedValue(busy);
    const changed = vi.fn();
    render(<SelectorSetup projectId="a" choice={choice({ kind: 'engine_setup', scope: 'organization', setup_ref: 'engine-setup:parakeet-tdt.local-onnx@1', can_mutate: true, can_start: false, blocked_by_operation: busy })} onChanged={changed} onEditingChange={vi.fn()} />);
    expect(screen.getByText('Another model')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow');
    expect(screen.queryByRole('button', { name: 'Download and set up' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(request).toHaveBeenCalledWith('outer.cancel_org_model_pull.post', expect.objectContaining({ pathParams: { pull_id: 7 }, signal: expect.any(AbortSignal) }));
    expect(screen.getByRole('button', { name: 'Cancelling…' })).toBeDisabled();
    await waitFor(() => expect(request).toHaveBeenCalledWith('outer.get_org_model_pull.get', expect.objectContaining({ pathParams: { pull_id: 7 } })), { timeout: 1800 });
    expect(changed).not.toHaveBeenCalled();
    expect(screen.queryByText('Cancelled.')).not.toBeInTheDocument();
  });

  it('retains advanced progress and cancellation after a failed poll, then recovers', async () => {
    vi.useFakeTimers();
    const initial = pull({ completed_bytes: 100, total_bytes: 1000 });
    const advanced = pull({ completed_bytes: 600, total_bytes: 1000 });
    request.mockResolvedValueOnce(advanced).mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error('GET failed'))
      .mockResolvedValueOnce(pull({ completed_bytes: 800, total_bytes: 1000, cancel_requested: true }));
    const changed = vi.fn();
    render(<SelectorSetup projectId="a" choice={choice({ kind: 'engine_setup', scope: 'organization', setup_ref: initial.model, can_mutate: true, can_start: false, blocked_by_operation: initial })} onChanged={changed} onEditingChange={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '600');
    expect(screen.getByRole('progressbar')).toHaveAttribute('max', '1000');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })); });
    expect(screen.getByRole('button', { name: 'Cancelling…' })).toBeDisabled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '600');
    expect(screen.getByRole('button', { name: 'Cancelling…' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('Could not refresh setup progress');
    expect(changed).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '800');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(request).toHaveBeenCalledWith('outer.cancel_org_model_pull.post', expect.objectContaining({ pathParams: { pull_id: initial.id } }));
  });

  it.each(['done', 'failed'] as const)('notifies a standalone setup parent when its pull becomes %s', async (status) => {
    vi.useFakeTimers();
    const terminal = pull({
      status,
      capabilities: { cancel: false, retry: status === 'failed', remove: false },
      error: status === 'failed' ? { code: 'failed', message: 'Download interrupted' } : null,
      finished_at: '2026-09-12T00:00:04Z',
    });
    request.mockResolvedValue(terminal);
    const changed = vi.fn();
    render(<SelectorSetup projectId="a" choice={choice({ kind: 'engine_setup', scope: 'workspace', setup_ref: terminal.model, can_mutate: true, can_start: false, blocked_by_operation: pull() })} onChanged={changed} onEditingChange={vi.fn()} />);

    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(changed).toHaveBeenCalledTimes(1);
  });

  it('starts explicit engine setup with its returned durable operation, without assuming readiness', async () => {
    request.mockResolvedValue({ pull: pull(), deduplicated: false });
    const changed = vi.fn();
    render(<SelectorSetup projectId="a" choice={choice({ kind: 'engine_setup', scope: 'workspace', setup_ref: 'engine-setup:parakeet-tdt.local-onnx@1', can_mutate: true, can_start: true, blocked_by_operation: null })} onChanged={changed} onEditingChange={vi.fn()} />);
    expect(request).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Download and set up' }));
    expect(request).toHaveBeenCalledWith('tenant.setup_model_engine.post', expect.objectContaining({ body: { setup_ref: 'engine-setup:parakeet-tdt.local-onnx@1' }, signal: expect.any(AbortSignal) }));
    expect(await screen.findByTestId('model-pull-progress')).toHaveAttribute('data-status', 'running');
    expect(changed).toHaveBeenCalledOnce();
    expect(screen.queryByText(/installed/)).not.toBeInTheDocument();
  });

  it('discards an old download start after switching inspected choices', async () => {
    let resolve: (value: { pull: ModelPullDto; deduplicated: boolean }) => void = () => {};
    request.mockImplementation(() => new Promise((done) => { resolve = done; }));
    const changed = vi.fn();
    const selected = choice({ kind: 'engine_setup', scope: 'workspace', setup_ref: 'engine-setup:parakeet-tdt.local-onnx@1', can_mutate: true, can_start: true, blocked_by_operation: null });
    const view = render(<SelectorSetup projectId="a" choice={selected} onChanged={changed} onEditingChange={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Download and set up' }));
    const signal = request.mock.calls[0][1].signal;
    view.rerender(<SelectorSetup projectId="a" choice={choice({ kind: 'first_use_download', disclosure: 'Downloads the required language model on first use.' })} onChanged={changed} onEditingChange={vi.fn()} />);
    expect(signal.aborted).toBe(true);
    await act(async () => resolve({ pull: pull(), deduplicated: false }));
    expect(screen.queryByTestId('model-pull-progress')).not.toBeInTheDocument();
    expect(changed).not.toHaveBeenCalled();
    expect(screen.getByText('Downloads the required language model on first use.')).toBeInTheDocument();
  });

  it('fetches the typed busy operation from a start conflict on the actual workspace scope', async () => {
    const active = pull({ id: 42, model: 'hf:another-model@revision', display_name: 'Another model' });
    request.mockRejectedValueOnce(new ModelSetupRequestError(409, 42)).mockResolvedValue(active);
    render(<SelectorSetup projectId="a" choice={choice({ kind: 'engine_setup', scope: 'workspace', setup_ref: 'engine-setup:parakeet-tdt.local-onnx@1', can_mutate: true, can_start: true, blocked_by_operation: null })} onChanged={vi.fn()} onEditingChange={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Download and set up' }));
    expect(await screen.findByText('Another model')).toBeInTheDocument();
    expect(request).toHaveBeenNthCalledWith(2, 'tenant.get_model_pull.get', expect.objectContaining({ pathParams: { pull_id: 42 }, signal: expect.any(AbortSignal) }));
    expect(screen.queryByRole('button', { name: 'Retry setup' })).not.toBeInTheDocument();
  });

  it('starts an organization artifact with its declared ref and retries only a matching authorized failed operation', async () => {
    const failed = pull({ operation_kind: 'artifact', model: 'hf:test/model@revision', status: 'failed',
      capabilities: { cancel: false, retry: true, remove: false }, error: { code: 'failed', message: 'Download interrupted' } });
    request.mockResolvedValue({ pull: pull({ model: failed.model, operation_kind: 'artifact' }), deduplicated: true });
    render(<SelectorSetup projectId="a" choice={choice({ kind: 'artifact_download', scope: 'organization', setup_ref: failed.model, can_mutate: true, can_start: true, blocked_by_operation: failed })} onChanged={vi.fn()} onEditingChange={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Retry setup' }));
    expect(request).toHaveBeenCalledWith('outer.post_org_models_pull.post', expect.objectContaining({ body: { ref: failed.model, unpinned_acknowledged: false }, signal: expect.any(AbortSignal) }));
    expect(await screen.findByTestId('model-pull-progress')).toHaveAttribute('data-status', 'running');
  });

  it('invalidates a rejected save receipt, preserves the draft key, and does not publish a successful change', async () => {
    request.mockResolvedValueOnce(accepted).mockRejectedValueOnce(new Error('expired receipt'));
    const changed = vi.fn();
    render(<SelectorSetup projectId="a" choice={choice({ kind: 'api_key', provider: 'openai', scopes: [scope('project')] })} onChanged={changed} onEditingChange={vi.fn()} />);
    await userEvent.type(screen.getByLabelText('API key'), 'draft-key');
    await userEvent.click(screen.getByRole('button', { name: 'Test' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not confirm the save');
    expect(screen.getByLabelText('API key')).toHaveValue('draft-key');
    expect(screen.getByRole('button', { name: 'Test' })).toBeEnabled();
    expect(changed).not.toHaveBeenCalled();
  });
});
