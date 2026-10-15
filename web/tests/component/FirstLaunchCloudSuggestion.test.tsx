// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FirstLaunchCloudSuggestion } from '../../src/components/FirstLaunchCloudSuggestion';
import { installDialogPolyfill } from '../support/domPolyfills';

const api = vi.hoisted(() => ({
  getConnection: vi.fn(), listProviders: vi.fn(), startAuthorization: vi.fn(),
  pollAuthorization: vi.fn(), cancelAuthorization: vi.fn(), changed: vi.fn(),
}));
vi.mock('../../src/api/cloudConnection', () => ({ cloudConnectionApi: api }));
vi.mock('../../src/api/open', () => ({ listProviders: api.listProviders }));
vi.mock('../../src/cloud/connectionChanged', () => ({ notifyCloudConnectionChanged: api.changed }));
const disconnected = { state: 'disconnected', identity: null, accounts: [],
  capabilities: { canConnect: true, canReconnect: false, canDisconnect: false } };
const connected = { ...disconnected, state: 'connected',
  identity: { id: 1, displayName: 'Reporter', email: 'reporter@example.test' } };

beforeEach(() => {
  vi.resetAllMocks();
  installDialogPolyfill();
  localStorage.clear();
  api.getConnection.mockResolvedValue(disconnected);
  api.listProviders.mockResolvedValue({ providers: [] });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('optional first-launch Cloud suggestion', () => {
  it('offers shared browser sign-in without starting authorization; remembers skip', async () => {
    const done = vi.fn();
    const view = render(<FirstLaunchCloudSuggestion onComplete={done} />);
    expect(await screen.findByRole('button', { name: 'Sign in with browser' })).toBeVisible();
    expect(api.startAuthorization).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Continue without signing in' }));
    expect(done).toHaveBeenCalledOnce();
    view.unmount();
    api.getConnection.mockClear();
    render(<FirstLaunchCloudSuggestion onComplete={done} />);
    expect(done).toHaveBeenCalledTimes(2);
    expect(api.getConnection).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('allows Escape to skip', async () => {
    const done = vi.fn();
    render(<FirstLaunchCloudSuggestion onComplete={done} />);
    fireEvent(await screen.findByRole('dialog'), new Event('cancel', { bubbles: true }));
    expect(done).toHaveBeenCalledOnce();
  });

  it.each([
    ['Cloud already connected', connected, { providers: [] }],
    ['Cloud unsupported', { ...disconnected, capabilities: { canConnect: false } }, { providers: [] }],
    ['own API key', disconnected, { providers: [{ kind: 'platform_api', configured: true }] }],
    ['local model', disconnected, { providers: [{ kind: 'local_http', reachable: true, models: ['local'] }] }],
    ['offline mode', disconnected, { providers: [], network: 'off' }],
  ])('does not interrupt users with %s', async (_name, connection, catalog) => {
    api.getConnection.mockResolvedValue(connection);
    api.listProviders.mockResolvedValue(catalog);
    const done = vi.fn();
    render(<FirstLaunchCloudSuggestion onComplete={done} />);
    await waitFor(() => expect(done).toHaveBeenCalledOnce());
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('does not block onboarding on a failed check', async () => {
    api.getConnection.mockRejectedValue(new Error('offline'));
    const done = vi.fn();
    render(<FirstLaunchCloudSuggestion onComplete={done} />);
    await waitFor(() => expect(done).toHaveBeenCalledOnce());
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('bounds the optional check and ignores a late response', async () => {
    vi.useFakeTimers();
    let resolve!: (value: unknown) => void;
    api.getConnection.mockReturnValue(new Promise((done) => { resolve = done; }));
    const done = vi.fn();
    render(<FirstLaunchCloudSuggestion onComplete={done} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(done).toHaveBeenCalledOnce();
    await act(async () => { resolve(disconnected); });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('completes the existing device flow and refreshes selectors', async () => {
    vi.useFakeTimers();
    api.startAuthorization.mockResolvedValue({ id: 'device', userCode: 'ABCD',
      verificationUri: 'https://app.frisket.dev/device', expiresAt: '2099-01-01T00:00:00Z',
      pollIntervalSeconds: 1 });
    api.pollAuthorization.mockResolvedValue({ status: 'connected', connection: connected });
    const done = vi.fn();
    render(<FirstLaunchCloudSuggestion onComplete={done} />);
    await act(async () => {});
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sign in with browser' })); });
    expect(screen.getByText('ABCD')).toBeVisible();
    expect(done).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(done).toHaveBeenCalledOnce();
    expect(api.changed).toHaveBeenCalledOnce();
  });
});
