// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { CloudConnectionClient } from '../../src/cloud/CloudConnectionPanel';
import { CloudProfileSettings } from '../../src/cloud/CloudProfileSettings';

afterEach(cleanup);

describe('CloudProfileSettings', () => {
  it('keeps installation settings focused on the device connection', async () => {
    const client: CloudConnectionClient = {
      getConnection: vi.fn().mockResolvedValue({
        state: 'connected',
        identity: { id: 7, displayName: 'Sam', email: 'sam@example.test' },
        accounts: [],
        capabilities: { canConnect: false, canReconnect: false, canDisconnect: true },
      }),
      startAuthorization: vi.fn(),
      pollAuthorization: vi.fn(),
      cancelAuthorization: vi.fn(),
      disconnect: vi.fn(),
    };

    render(<CloudProfileSettings connectionClient={client} />);

    expect(await screen.findByText('Sam')).toBeVisible();
    expect(screen.getByRole('button', { name: /Sign out/ })).toBeVisible();
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
  });
});
