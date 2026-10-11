// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  CloudConnectionPanel,
  type CloudConnectionClient,
  type CloudConnectionView,
} from '../../src/cloud/CloudConnectionPanel';

const disconnected = (): CloudConnectionView => ({
  state: 'disconnected', identity: null, accounts: [],
  capabilities: { canConnect: true, canReconnect: false, canDisconnect: false },
});
const connected = (): CloudConnectionView => ({
  state: 'connected', identity: { id: 7, displayName: 'Sam Rivera', email: 'sam@example.com' },
  accounts: [
    { id: 11, name: 'Riverton News', kind: 'organization', eligible: true, balanceLabel: '$182.40 balance' },
    { id: 12, name: 'Personal', kind: 'personal', eligible: true, balanceLabel: '$5.00 credits' },
  ],
  capabilities: { canConnect: false, canReconnect: false, canDisconnect: true },
});

function client(overrides: Partial<CloudConnectionClient> = {}): CloudConnectionClient {
  return {
    getConnection: vi.fn().mockResolvedValue(disconnected()),
    startAuthorization: vi.fn().mockResolvedValue({ id: 'device-1', userCode: 'KQ4F-7M2D',
      verificationUri: 'https://app.frisket.dev/device', expiresAt: '2026-10-11T03:00:00Z', pollIntervalSeconds: 1 }),
    pollAuthorization: vi.fn().mockResolvedValue({ status: 'connected', connection: connected() }),
    cancelAuthorization: vi.fn().mockResolvedValue(undefined),
    disconnect: vi.fn().mockResolvedValue(disconnected()),
    ...overrides,
  };
}

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe('CloudConnectionPanel', () => {
  it('completes the shared device flow and exposes installation sign-out only in Profile', async () => {
    vi.useFakeTimers();
    vi.spyOn(window, 'open').mockReturnValue(null);
    const api = client();
    render(<CloudConnectionPanel client={api} variant="profile" allowDisconnect />);
    await act(async () => {});
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /sign in with browser/i })); });
    expect(screen.getByText('KQ4F-7M2D')).toBeVisible();
    expect(window.open).toHaveBeenCalledWith('https://app.frisket.dev/device', '_blank', 'noopener,noreferrer');
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByTestId('cloud-connection-connected')).toHaveTextContent('Sam Rivera');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Disconnect' })); });
    expect(api.disconnect).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', { name: /sign in with browser/i })).toBeVisible();
  });

  it('keeps the project account step in context and never offers Disconnect inline', async () => {
    vi.useFakeTimers();
    vi.spyOn(window, 'open').mockReturnValue(null);
    const api = client();
    const selectAccount = vi.fn().mockResolvedValue(undefined);
    render(<CloudConnectionPanel client={api} variant="compact" onSelectAccount={selectAccount} />);
    await act(async () => {});
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /sign in with browser/i })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('heading', { name: 'Which account should pay?' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Disconnect' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: /Personal/ }));
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Continue' })); });
    expect(selectAccount).toHaveBeenCalledWith(12);
    expect(screen.getByText('This project can now use Frisket Cloud.')).toBeVisible();
  });

  it('returns a declined authorization to the connect card with one actionable error', async () => {
    vi.useFakeTimers();
    vi.spyOn(window, 'open').mockReturnValue(null);
    const api = client({
      pollAuthorization: vi.fn().mockResolvedValue({ status: 'declined', connection: disconnected() }),
    });
    render(<CloudConnectionPanel client={api} variant="compact" />);
    await act(async () => {});
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /sign in with browser/i })); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('alert')).toHaveTextContent('Sign-in didn’t finish');
    expect(screen.getByRole('button', { name: /sign in with browser/i })).toBeVisible();
  });
});
