// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ProjectInfo, RuntimeConfig } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { ProjectServiceSettings } from '../../src/settings/ProjectServiceSettings';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { runtimeConfig } from '../support/domainFixtures';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const { getRuntimeConfig } = vi.hoisted(() => ({
  getRuntimeConfig: vi.fn<() => Promise<RuntimeConfig>>(),
}));

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  getRuntimeConfig,
}));

vi.mock('../../src/shellIdentity', () => ({
  useShellIdentity: () => ({ identityMode: false, me: null, resolved: true }),
}));

vi.mock('../../src/telemetry/productTelemetry', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/telemetry/productTelemetry')>(),
  workspaceSharesUsageData: () => true,
}));

const projectApi = createProjectApi('alpha');
const getProject = vi.spyOn(projectApi, 'getProject');
const updateAllowed = vi.spyOn(projectApi, 'updateCurrentProjectAllowedServices');
const updateUsage = vi.spyOn(projectApi, 'updateCurrentProjectUsageDataSharing');

const { render } = createWorkspaceTestHarness({ projectId: 'alpha', api: { projectApi } });

const project = (overrides: Partial<ProjectInfo> = {}): ProjectInfo => ({
  id: 'alpha',
  name: 'Alpha',
  role: 'owner',
  allowed_services: 'this_machine_only',
  usage_data_sharing: 'inherit',
  ...overrides,
});

beforeAll(() => {
  installPopoverPolyfill();
});

beforeEach(() => {
  getRuntimeConfig.mockResolvedValue(runtimeConfig());
  getProject.mockResolvedValue(project());
  updateAllowed.mockImplementation(async (value) => project({ allowed_services: value }));
  updateUsage.mockImplementation(async (value) => project({ usage_data_sharing: value }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Project Allowed services and usage-data settings', () => {
  it('asks before loosening, naming the old and new value, then confirms with the new value', async () => {
    render(<ProjectServiceSettings canMutate />);
    fireEvent.click(await screen.findByRole('radio', { name: /All services/ }));

    const confirm = await screen.findByTestId('allowed-services-confirm');
    expect(confirm).toHaveTextContent('Change Allowed services from This machine only to All services?');
    expect(updateAllowed).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('allowed-services-confirm-button'));
    await waitFor(() => expect(updateAllowed).toHaveBeenCalledWith('all_services', 'all_services'));
    await waitFor(() => expect(screen.getByRole('radio', { name: /All services/ })).toBeChecked());
  });

  it('keeps the setting when the loosening is cancelled', async () => {
    render(<ProjectServiceSettings canMutate />);
    fireEvent.click(await screen.findByRole('radio', { name: /Approved services/ }));
    fireEvent.click(await screen.findByTestId('allowed-services-confirm-cancel'));
    await waitFor(() => expect(screen.queryByTestId('allowed-services-confirm')).toBeNull());
    expect(updateAllowed).not.toHaveBeenCalled();
    expect(screen.getByRole('radio', { name: /This machine only/ })).toBeChecked();
  });

  it('applies a stricter value at once without confirmation', async () => {
    getProject.mockResolvedValue(project({ allowed_services: 'all_services' }));
    render(<ProjectServiceSettings canMutate />);
    fireEvent.click(await screen.findByRole('radio', { name: /Approved services/ }));
    await waitFor(() => expect(updateAllowed).toHaveBeenCalledWith('approved_services', null));
    expect(screen.queryByTestId('allowed-services-confirm')).toBeNull();
  });

  it('shows the error the server gives', async () => {
    getProject.mockResolvedValue(project({ allowed_services: 'all_services' }));
    updateAllowed.mockRejectedValue(new Error('project owner required'));
    render(<ProjectServiceSettings canMutate />);
    fireEvent.click(await screen.findByRole('radio', { name: /This machine only/ }));
    expect(await screen.findByText('project owner required')).toBeInTheDocument();
  });

  it('changes the usage-data override between inherit, off and on', async () => {
    render(<ProjectServiceSettings canMutate />);
    expect(await screen.findByTestId('project-usage-data-sharing')).toHaveTextContent(
      'Follow the workspace setting (currently shares)',
    );
    fireEvent.change(screen.getByTestId('project-usage-data-sharing'), { target: { value: 'off' } });
    await waitFor(() => expect(updateUsage).toHaveBeenCalledWith('off'));
  });


  it('shows a stored value the edition no longer offers, then loosens through the confirmation', async () => {
    getRuntimeConfig.mockResolvedValue(runtimeConfig({ allowed_services_offered: ['approved_services', 'all_services'] }));
    render(<ProjectServiceSettings canMutate />);
    const current = await screen.findByRole('radio', { name: /This machine only/ });
    expect(current).toBeChecked();
    expect(screen.getByTestId('allowed-services-not-offered')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('radio', { name: /Approved services/ }));
    expect(await screen.findByTestId('allowed-services-confirm')).toHaveTextContent(
      'Change Allowed services from This machine only to Approved services?',
    );
    fireEvent.click(screen.getByTestId('allowed-services-confirm-button'));
    await waitFor(() => expect(updateAllowed).toHaveBeenCalledWith('approved_services', 'approved_services'));
    await waitFor(() => expect(screen.queryByRole('radio', { name: /This machine only/ })).toBeNull());
  });

  it('is read-only for a role that cannot change it', async () => {
    render(<ProjectServiceSettings canMutate={false} />);
    expect(await screen.findByRole('radio', { name: /All services/ })).toBeDisabled();
  });
});
