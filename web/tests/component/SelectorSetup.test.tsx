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
type Scope = Extract<Setup, { kind: 'api_key' }>['scopes'][number];
function scope(name: Scope['scope'], canMutate = true): Scope {
  return { scope: name, can_mutate: canMutate, configured: false,
    source: 'missing', hint: null, environment_names: [], settings_location: null };
}
function choice(setup: Setup): SelectorChoice {
  return { choice_id: 'model:openai/test', label: 'Test model', summary: '', description: '',
    facts: [], authored_selection: { kind: 'model', model: 'openai/test' },
    processing_destination: { kind: 'external', label: 'Provider' }, resolved_target: null,
    is_current: false, is_default: false, can_author: true, can_run: false,
    status: 'needs_setup', blocker: null, active_operation: null, model_card_url: null, setup };
}
const accepted = { provider: 'openai', ok: true, reachable: true, status: 200,
  detail: null, validation_token: 'signed-key-receipt' };
const gatewayAccepted = { schemaVersion: 'frisket.models_gateway_validation.v1',
  normalized_origin: 'https://models.test', validation_token: 'signed-gateway-receipt',
  probe: { ok: true, reachable: true, status: 200, detail: null,
    service: 'frisket-models', version: null, engines: [] } };

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
  it('validates then turns the compact project-key action into Save with its real receipt', async () => {
    request.mockResolvedValueOnce(accepted).mockResolvedValueOnce({});
    const changed = vi.fn();
    render(<SelectorSetup projectId="project-a" choice={choice({ kind: 'api_key', provider: 'openai', scopes: [scope('project')] })} onChanged={changed} onEditingChange={vi.fn()} />);
    const key = screen.getByLabelText('API key');
    expect(key).toHaveAttribute('placeholder', 'API key');
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

  it('uses an authorized gateway scope without showing diagnostics or a scope picker', async () => {
    request.mockResolvedValue(gatewayAccepted);
    const environment = { ...scope('environment', false), source: 'environment' as const,
      configured: true, environment_names: ['FRISKET_MODELS_URL', 'FRISKET_MODELS_TOKEN'] };
    render(<SelectorSetup projectId="project-a" choice={choice({ kind: 'models_gateway', scopes: [environment, scope('organization')] })} onChanged={vi.fn()} onEditingChange={vi.fn()} />);
    expect(screen.queryByText(/FRISKET_MODELS_URL/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Credential scope')).not.toBeInTheDocument();
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
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '60');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })); });
    expect(screen.getByRole('button', { name: 'Cancelling…' })).toBeDisabled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '60');
    expect(screen.getByRole('button', { name: 'Cancelling…' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('Could not refresh setup progress');
    expect(changed).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '80');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(request).toHaveBeenCalledWith('outer.cancel_org_model_pull.post', expect.objectContaining({ pathParams: { pull_id: initial.id } }));
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
