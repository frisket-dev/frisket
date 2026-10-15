// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { HttpSelectorChoicesResponse } from '../../src/api/selectorChoices';
import { cloudConnectionApi, projectCloudRoutingApi } from '../../src/api/cloudConnection';
import { AskModelSetup } from '../../src/components/project-ask/AskModelSetup';
import { notifyCloudConnectionChanged } from '../../src/cloud/connectionChanged';
import { installDialogPolyfill } from '../support/domPolyfills';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('../../src/api/httpContract', () => ({ httpContract: request }));
vi.mock('../../src/api/cloudConnection', () => ({
  cloudConnectionApi: {
    getConnection: vi.fn(), startAuthorization: vi.fn(), pollAuthorization: vi.fn(),
    cancelAuthorization: vi.fn(), disconnect: vi.fn(),
  },
  projectCloudRoutingApi: { get: vi.fn(), patch: vi.fn() },
}));
beforeAll(installDialogPolyfill);
beforeEach(() => {
  vi.mocked(cloudConnectionApi.getConnection).mockResolvedValue({
    state: 'disconnected', identity: null, accounts: [],
    capabilities: { canConnect: true, canReconnect: false, canDisconnect: false },
  });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks(); localStorage.clear(); });

function response(ready = false): HttpSelectorChoicesResponse {
  return {
    schema_version: 'frisket.selector_choices.v2', project_id: 'project-a',
    subject: { kind: 'project_ask' }, depends_on: [], current_choice_id: 'chat-model',
    default_choice_id: 'chat-model', orphaned_current: null,
    cloud_setup: { kind: 'cloud', state: 'connect' },
    groups: [{ group_id: 'models', label: 'Models', kind: 'provider', status: ready ? 'ready' : 'needs_setup',
      choices: [{
        choice_id: 'chat-model', label: 'Chat model', summary: '', description: '',
        model_card_url: null, authored_selection: { kind: 'model', model: 'provider/chat-model' },
        resolved_target: null, processing_destination: { kind: 'external', label: 'Provider' }, facts: [],
        status: ready ? 'ready' : 'needs_setup', can_author: true, can_run: ready,
        blocker: null, execution_route: null, route_selection: null, setup: null,
        configuration_actions: [], active_operation: null, is_default: true, is_current: true,
      }],
    }],
  };
}

function mount(model: string | null = null) {
  const onSelect = vi.fn();
  const current = vi.fn();
  const view = render(<AskModelSetup projectId="project-a" model={model}
    onSelect={onSelect} onCurrentChoiceChange={current} />);
  return { ...view, onSelect, current };
}

describe('Ask model setup', () => {
  it('offers Cloud sign-in and the existing model picker, using one selector query', async () => {
    request.mockResolvedValue(response());
    mount();
    expect(await screen.findByRole('button', { name: 'Sign in to Frisket Cloud' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Sign in with browser' })).not.toBeInTheDocument();
    expect(cloudConnectionApi.getConnection).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Set up a model' }));
    expect(screen.getByTestId('engine-selector-dialog')).toBeVisible();
    expect(request.mock.calls.filter(([id]) => id === 'tenant.selector_choices.post')).toHaveLength(1);
  });

  it('leaves configured users alone and keeps their current model visible', async () => {
    request.mockResolvedValue(response(true));
    const { current, onSelect } = mount('provider/chat-model');
    await screen.findByRole('button', { name: /Chat model/ });
    await waitFor(() => expect(current).toHaveBeenLastCalledWith(expect.objectContaining({ can_run: true })));
    expect(screen.queryByText(/start chatting/)).not.toBeInTheDocument();
    expect(cloudConnectionApi.getConnection).not.toHaveBeenCalled();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('does not interpret loading or a failed projection as missing configuration', async () => {
    let reject!: (error: Error) => void;
    request.mockReturnValue(new Promise((_, fail) => { reject = fail; }));
    mount();
    expect(screen.getByText('Loading choices…')).toBeVisible();
    expect(screen.queryByText(/start chatting/)).not.toBeInTheDocument();
    await act(async () => reject(new Error('Temporarily unavailable')));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load choices');
    expect(screen.queryByText(/start chatting/)).not.toBeInTheDocument();
    expect(cloudConnectionApi.getConnection).not.toHaveBeenCalled();
  });

  it('does not invent a Cloud setup offer when the backend omits it', async () => {
    const payload = response();
    payload.cloud_setup = null;
    request.mockResolvedValue(payload);
    mount();
    expect(await screen.findByRole('button', { name: 'Set up a model' })).toBeVisible();
    expect(screen.queryByText(/Sign in to Frisket Cloud/)).not.toBeInTheDocument();
    expect(cloudConnectionApi.getConnection).not.toHaveBeenCalled();
  });

  it('does not offer sign-in for an out-of-credits account', async () => {
    const payload = response();
    payload.cloud_setup = { kind: 'cloud', state: 'out_of_credits' };
    request.mockResolvedValue(payload);
    mount();
    await screen.findByRole('button', { name: 'Set up a model' });
    expect(cloudConnectionApi.getConnection).not.toHaveBeenCalled();
  });

  it('offers a ready alternative without silently replacing the saved selection', async () => {
    const payload = response();
    payload.groups[0].choices.push({ ...payload.groups[0].choices[0], choice_id: 'other-model',
      label: 'Available model', authored_selection: { kind: 'model', model: 'provider/other-model' },
      status: 'ready', can_run: true, is_current: false, is_default: false });
    request.mockResolvedValue(payload);
    const { onSelect } = mount('provider/chat-model');
    const choose = await screen.findByRole('button', { name: 'Choose a model' });
    expect(cloudConnectionApi.getConnection).not.toHaveBeenCalled();
    fireEvent.click(choose);
    const dialog = screen.getByTestId('engine-selector-dialog');
    expect(within(dialog).getByRole('button', { name: /Available model/ })).toBeVisible();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('allows Cloud setup even when no models are listed yet, without an inert model button', async () => {
    const payload = response();
    payload.groups = []; payload.current_choice_id = null; payload.default_choice_id = null;
    request.mockResolvedValue(payload);
    mount();
    const signIn = await screen.findByRole('button', { name: 'Sign in to Frisket Cloud' });
    fireEvent.click(signIn);
    expect(await screen.findByRole('button', { name: 'Sign in with browser' })).toBeVisible();
    expect(signIn).toHaveAttribute('aria-expanded', 'true');
    expect(screen.queryByRole('button', { name: 'Set up a model' })).not.toBeInTheDocument();
  });

  it('refreshes the authoritative model after browser sign-in and project account selection', async () => {
    vi.useFakeTimers();
    request.mockResolvedValueOnce(response()).mockResolvedValue(response(true));
    vi.mocked(cloudConnectionApi.startAuthorization).mockResolvedValue({
      id: 'authorization', userCode: 'ABCD', verificationUri: 'https://example.test/authorize',
      expiresAt: '2030-01-01', pollIntervalSeconds: 1,
    });
    vi.mocked(cloudConnectionApi.pollAuthorization).mockResolvedValue({ status: 'connected', connection: {
      state: 'connected', identity: { id: 1, displayName: 'Reader', email: 'reader@example.test' },
      accounts: [{ id: 7, name: 'Personal', kind: 'personal', eligible: true }],
      capabilities: { canConnect: false, canReconnect: true, canDisconnect: true },
    } });
    vi.mocked(projectCloudRoutingApi.patch).mockResolvedValue({ connectionState: 'connected',
      selectedAccountId: 7, accounts: [], cloudProviderKeys: [], canMutate: true });
    let view!: ReturnType<typeof mount>;
    await act(async () => { view = mount('provider/chat-model'); });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sign in to Frisket Cloud' })); });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sign in with browser' })); });
    expect(screen.getByText('ABCD')).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(projectCloudRoutingApi.patch).toHaveBeenCalledWith('project-a', { selectedAccountId: 7 });
    expect(request.mock.calls.filter(([id]) => id === 'tenant.selector_choices.post')).toHaveLength(2);
    expect(view.current).toHaveBeenLastCalledWith(expect.objectContaining({ can_run: true }));
    expect(view.onSelect).not.toHaveBeenCalled();
    expect(screen.queryByText(/start chatting/)).not.toBeInTheDocument();
  });

  it('refreshes when Cloud setup completes elsewhere without replacing a saved model', async () => {
    request.mockResolvedValueOnce(response()).mockResolvedValue(response(true));
    const { onSelect, current, unmount } = mount('provider/chat-model');
    await screen.findByRole('button', { name: 'Set up a model' });
    await act(async () => { notifyCloudConnectionChanged(); });
    await waitFor(() => expect(current).toHaveBeenLastCalledWith(expect.objectContaining({ can_run: true })));
    expect(onSelect).not.toHaveBeenCalled();
    const queries = request.mock.calls.filter(([id]) => id === 'tenant.selector_choices.post');
    expect(queries).toHaveLength(2);
    expect(queries[1][1].body.subject).toEqual({ kind: 'project_ask', model: 'provider/chat-model' });
    unmount();
    await act(async () => { notifyCloudConnectionChanged(); });
    expect(request.mock.calls.filter(([id]) => id === 'tenant.selector_choices.post')).toHaveLength(2);
  });
});
