// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest';

import type { MeInfo, RuntimeConfig } from '../../src/api/types';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    getMe: vi.fn(),
    getRuntimeConfig: vi.fn(),
    updateProfile: vi.fn(),
    updateLocalCostPreapproval: vi.fn(),
  };
});

import {
  getMe,
  getRuntimeConfig,
  updateLocalCostPreapproval,
  updateProfile,
} from '../../src/api/open';
import { PersonalProfileSettings } from '../../src/settings/SettingsSections';

const runtimeConfig = (overrides: Partial<RuntimeConfig> = {}): RuntimeConfig => ({
  cache_mode: 'replay',
  live_calls_possible: true,
  cache_mode_editable: true,
  cost_preapproval_usd: '2',
  cost_preapproval_editable: true,
  email_from_address: null,
  email_from_name: null,
  auth_methods: { password: true, magic_link: false, oidc: [] },
  plugins_available: false,
  plugin_management_available: false,
  product_telemetry_available: false,
  ...overrides,
});

const profile = (overrides: Partial<MeInfo> = {}): MeInfo => ({
  email: 'person@example.com',
  display_name: 'Person',
  cost_preapproval_usd: null,
  ...overrides,
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('personal profile pre-approval settings', () => {
  it('patches only the edited name, retaining an unanswered pre-approval amount', async () => {
    (getMe as Mock).mockResolvedValue(profile());
    (getRuntimeConfig as Mock).mockResolvedValue(runtimeConfig());
    (updateProfile as Mock).mockResolvedValue(profile({ display_name: 'Renamed' }));

    render(<PersonalProfileSettings identityMode />);
    const name = await screen.findByTestId('personal-display-name');
    expect(screen.getByText(/use their selected provider without another prompt/i)).toBeInTheDocument();
    fireEvent.change(name, { target: { value: 'Renamed' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(updateProfile).toHaveBeenCalledWith({ display_name: 'Renamed' }));
    expect(updateProfile).not.toHaveBeenCalledWith(expect.objectContaining({ cost_preapproval_usd: '2' }));
  });

  it('waits for the local runtime amount and does not overwrite a saved zero when saving a name', async () => {
    let resolveRuntime!: (value: RuntimeConfig) => void;
    const pendingRuntime = new Promise<RuntimeConfig>((resolve) => { resolveRuntime = resolve; });
    (getMe as Mock).mockResolvedValue(profile({ email: 'Local workspace', display_name: '' }));
    (getRuntimeConfig as Mock).mockReturnValue(pendingRuntime);

    render(<PersonalProfileSettings identityMode={false} />);
    const save = await screen.findByRole('button', { name: /^save$/i });
    expect(save).toBeDisabled();
    expect(updateLocalCostPreapproval).not.toHaveBeenCalled();

    resolveRuntime(runtimeConfig({ cost_preapproval_usd: '0' }));
    const amount = await screen.findByTestId('personal-cost-preapproval');
    await waitFor(() => expect(amount).toHaveValue(0));
    fireEvent.change(screen.getByTestId('personal-display-name'), { target: { value: 'Desk' } });
    fireEvent.click(save);

    await waitFor(() => expect(screen.getByText('Profile saved')).toBeInTheDocument());
    expect(updateLocalCostPreapproval).not.toHaveBeenCalled();
  });

  it('retains a saved local amount and does not resubmit it with a later name save', async () => {
    (getMe as Mock).mockResolvedValue(profile({ email: 'Local workspace', display_name: '' }));
    (getRuntimeConfig as Mock).mockResolvedValue(runtimeConfig({ cost_preapproval_usd: '0' }));
    (updateLocalCostPreapproval as Mock).mockResolvedValue(
      runtimeConfig({ cost_preapproval_usd: '3.25' }),
    );

    render(<PersonalProfileSettings identityMode={false} />);
    const amount = await screen.findByTestId('personal-cost-preapproval');
    await waitFor(() => expect(amount).toHaveValue(0));
    fireEvent.change(amount, { target: { value: '3.25' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(updateLocalCostPreapproval).toHaveBeenCalledWith('3.25'));
    expect(amount).toHaveValue(3.25);
    fireEvent.change(screen.getByTestId('personal-display-name'), { target: { value: 'Desk' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(screen.getByText('Profile saved')).toBeInTheDocument());
    expect(updateLocalCostPreapproval).toHaveBeenCalledTimes(1);
  });
});
