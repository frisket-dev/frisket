// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { CloudConnectionClient } from '../../src/cloud/CloudConnectionPanel';
import {
  ProjectCloudRoutingSettings,
  type ProjectCloudRoutingClient,
  type ProjectCloudRoutingView,
} from '../../src/cloud/ProjectCloudRoutingSettings';

const routing = (overrides: Partial<ProjectCloudRoutingView> = {}): ProjectCloudRoutingView => ({
  connectionState: 'connected', selectedAccountId: 11, preference: 'local_first',
  preferenceInherited: true, canMutate: true, overlapProviderLabels: ['Google'],
  accounts: [
    { id: 11, name: 'Riverton News', kind: 'organization', eligible: true, balanceLabel: '$182.40 balance', balanceUrl: 'https://app.frisket.dev/accounts/11' },
    { id: 12, name: 'Personal', kind: 'personal', eligible: true, balanceLabel: '$5.00 credits' },
  ],
  ...overrides,
});

const connectionClient: CloudConnectionClient = {
  getConnection: vi.fn(), startAuthorization: vi.fn(), pollAuthorization: vi.fn(),
  cancelAuthorization: vi.fn(), disconnect: vi.fn(),
};

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe('ProjectCloudRoutingSettings', () => {
  it('patches only the project account and overlap preference and never offers disconnect', async () => {
    const api: ProjectCloudRoutingClient = {
      get: vi.fn().mockResolvedValue(routing()),
      patch: vi.fn(async (_projectId, change) => routing({
        selectedAccountId: change.selectedAccountId ?? 11,
        preference: change.preference ?? 'local_first',
        preferenceInherited: false,
      })),
    };
    render(<ProjectCloudRoutingSettings projectId="project-a"
      connectionClient={connectionClient} routingClient={api} />);

    const account = await screen.findByRole('combobox', { name: /Cloud account for this project/ });
    fireEvent.change(account, { target: { value: '12' } });
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith('project-a',
      { selectedAccountId: 12 }, expect.any(AbortSignal)));
    fireEvent.click(screen.getByRole('radio', { name: /Use Personal/ }));
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith('project-a',
      { preference: 'cloud_first' }, expect.any(AbortSignal)));
    expect(screen.queryByRole('button', { name: /disconnect/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/sync/i)).not.toBeInTheDocument();
  });

  it('keeps a disconnected project preference visible while directing connection through the shared panel', async () => {
    const api: ProjectCloudRoutingClient = {
      get: vi.fn().mockResolvedValue(routing({ connectionState: 'disconnected', selectedAccountId: null })),
      patch: vi.fn(),
    };
    (connectionClient.getConnection as ReturnType<typeof vi.fn>).mockResolvedValue({
      state: 'disconnected', identity: null, accounts: [],
      capabilities: { canConnect: true, canReconnect: false, canDisconnect: false },
    });
    render(<ProjectCloudRoutingSettings projectId="project-a"
      connectionClient={connectionClient} routingClient={api} />);

    expect(await screen.findByRole('button', { name: /Sign in with browser/ })).toBeVisible();
    expect(screen.getByRole('radio', { name: /Use your keys/ })).toBeChecked();
    expect(screen.getByRole('radio', { name: /Use the project’s Cloud account/ })).toBeDisabled();
    expect(screen.queryByRole('button', { name: /disconnect/i })).not.toBeInTheDocument();
  });

  it('reconnects the installation without changing the project funding account', async () => {
    const api: ProjectCloudRoutingClient = {
      get: vi.fn().mockResolvedValue(routing({ connectionState: 'expired' })),
      patch: vi.fn(),
    };
    const reconnectClient: CloudConnectionClient = {
      getConnection: vi.fn().mockResolvedValue({
        state: 'expired', identity: null, accounts: [],
        capabilities: { canConnect: false, canReconnect: true, canDisconnect: false },
      }),
      startAuthorization: vi.fn().mockResolvedValue({ id: 'reconnect-1', userCode: 'ABCD-1234',
        verificationUri: 'https://app.frisket.dev/device', expiresAt: '2026-10-11T03:00:00Z', pollIntervalSeconds: 1 }),
      pollAuthorization: vi.fn().mockResolvedValue({
        status: 'connected',
        connection: {
          state: 'connected', identity: { id: 7, displayName: 'Sam', email: 'sam@example.test' },
          accounts: routing().accounts,
          capabilities: { canConnect: false, canReconnect: false, canDisconnect: true },
        },
      }),
      cancelAuthorization: vi.fn().mockResolvedValue(undefined),
      disconnect: vi.fn(),
    };
    render(<ProjectCloudRoutingSettings projectId="project-a"
      connectionClient={reconnectClient} routingClient={api} />);

    fireEvent.click(await screen.findByRole('button', { name: 'Reconnect' }));
    await screen.findByRole('link', { name: /Open sign-in page/ });
    await waitFor(() => expect(reconnectClient.pollAuthorization).toHaveBeenCalled(), {
      timeout: 2_000,
    });
    expect(api.patch).not.toHaveBeenCalled();
  });

  it('keeps an ineligible selected account visible and asks for an explicit replacement', async () => {
    const api: ProjectCloudRoutingClient = {
      get: vi.fn().mockResolvedValue(routing({
        preference: 'cloud_first',
        accounts: [{
          id: 11, name: 'Riverton News', kind: 'organization', eligible: false,
          eligibilityReason: 'Membership changed.', balanceLabel: '$182.40 balance',
        }, routing().accounts[1]],
      })),
      patch: vi.fn(),
    };
    render(<ProjectCloudRoutingSettings projectId="project-a"
      connectionClient={connectionClient} routingClient={api} />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Riverton News can no longer fund this project',
    );
    expect(screen.getByRole('combobox', { name: /Cloud account/ })).toHaveValue('11');
    expect(screen.getByRole('option', { name: /Riverton News.*unavailable/ })).toBeDisabled();
    expect(screen.getByRole('radio', { name: /Use Riverton News/ })).toBeDisabled();
    expect(api.patch).not.toHaveBeenCalled();
  });
});
