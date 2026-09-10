// @vitest-environment jsdom
//
// The Plugin Manager gate must fail CLOSED: while runtime config is unknown
// (still loading, or the fetch failed) it must not expose lifecycle controls.
// Contribution availability is separate: bundled-only compositions may load
// runtime layout while keeping lifecycle management hidden.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ProjectInfo } from '../../src/api/types';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    getRuntimeConfig: vi.fn(),
  };
});

import * as apiModule from '../../src/api/open';
import {
  ProjectPluginsSettings,
  ProjectSecretsSettings,
} from '../../src/settings/SettingsSections';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
const getWorkbenchPluginRuntimeIndex = vi.spyOn(api, 'getWorkbenchPluginRuntimeIndex');
const getProjectSecrets = vi.spyOn(api, 'getProjectSecrets');
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project',
  api: { projectApi: api },
});

const OWNER = { id: 'p1', name: 'P', role: 'owner' } as unknown as ProjectInfo;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ProjectPluginsSettings gate (fail closed)', () => {
  it('stays hidden and never calls the plugin-index route while config is still loading', async () => {
    vi.mocked(apiModule.getRuntimeConfig).mockReturnValue(new Promise(() => {}));
    render(<ProjectPluginsSettings identityMode={false} project={OWNER} />);

    expect(await screen.findByText('Plugin management is not available on this server.')).toBeInTheDocument();
    expect(getWorkbenchPluginRuntimeIndex).not.toHaveBeenCalled();
  });

  it('stays hidden and never calls the plugin-index route when the config fetch fails', async () => {
    vi.mocked(apiModule.getRuntimeConfig).mockRejectedValue(new Error('network error'));
    render(<ProjectPluginsSettings identityMode={false} project={OWNER} />);

    expect(await screen.findByText('Plugin management is not available on this server.')).toBeInTheDocument();
    await waitFor(() => expect(getWorkbenchPluginRuntimeIndex).not.toHaveBeenCalled());
  });

  it('shows the manager once config says plugin management is available', async () => {
    vi.mocked(apiModule.getRuntimeConfig).mockResolvedValue({
      cache_mode: 'replay',
      live_calls_possible: true,
      cache_mode_editable: false,
      email_from_address: null,
      email_from_name: null,
      plugins_available: true,
      plugin_management_available: true,
    } as Awaited<ReturnType<typeof apiModule.getRuntimeConfig>>);
    getWorkbenchPluginRuntimeIndex.mockResolvedValue({
      schemaVersion: 'frisket.workbench_plugin_runtime_index.v1',
      projectId: 'test-project',
      arbitraryPackageLoadAllowed: false,
      receiptScanLimit: 50,
      skippedInvalidReceipts: 0,
      skippedInvalidManifestRefs: 0,
      loadedPluginCount: 0,
      firstParty: { schemaVersion: 'frisket.workbench_descriptor_package.v1', descriptors: [] },
      plugins: [],
    });
    render(<ProjectPluginsSettings identityMode={false} project={OWNER} />);

    await waitFor(() => expect(getWorkbenchPluginRuntimeIndex).toHaveBeenCalled());
    expect(screen.queryByText('Plugin management is not available on this server.')).not.toBeInTheDocument();
  });

  it('hides lifecycle management for a bundled-only composition', async () => {
    vi.mocked(apiModule.getRuntimeConfig).mockResolvedValue({
      cache_mode: 'replay',
      live_calls_possible: true,
      cache_mode_editable: false,
      email_from_address: null,
      email_from_name: null,
      plugins_available: true,
      plugin_management_available: false,
    } as Awaited<ReturnType<typeof apiModule.getRuntimeConfig>>);
    render(<ProjectPluginsSettings identityMode={false} project={OWNER} />);

    expect(await screen.findByText('Plugin management is not available on this server.')).toBeInTheDocument();
    expect(getWorkbenchPluginRuntimeIndex).not.toHaveBeenCalled();
  });
});

describe('ProjectSecretsSettings plugin hint gate (fail closed)', () => {
  it('loads project secrets but never probes an absent plugin index', async () => {
    vi.mocked(apiModule.getRuntimeConfig).mockResolvedValue({
      cache_mode: 'replay',
      live_calls_possible: true,
      cache_mode_editable: false,
      email_from_address: null,
      email_from_name: null,
      plugins_available: false,
      plugin_management_available: false,
    } as Awaited<ReturnType<typeof apiModule.getRuntimeConfig>>);
    getProjectSecrets.mockResolvedValue({
      schemaVersion: 'frisket.project_secrets.v1',
      projectId: 'test-project',
      secrets: [],
      conflicts: [],
    });

    render(<ProjectSecretsSettings project={OWNER} />);

    await waitFor(() => expect(getProjectSecrets).toHaveBeenCalled());
    expect(getWorkbenchPluginRuntimeIndex).not.toHaveBeenCalled();
  });

  it('keeps bundled contribution secrets visible when management is unavailable', async () => {
    vi.mocked(apiModule.getRuntimeConfig).mockResolvedValue({
      cache_mode: 'replay',
      live_calls_possible: true,
      cache_mode_editable: false,
      email_from_address: null,
      email_from_name: null,
      plugins_available: true,
      plugin_management_available: false,
    } as Awaited<ReturnType<typeof apiModule.getRuntimeConfig>>);
    getProjectSecrets.mockResolvedValue({
      schemaVersion: 'frisket.project_secrets.v1',
      projectId: 'test-project',
      secrets: [],
      conflicts: [],
    });
    getWorkbenchPluginRuntimeIndex.mockResolvedValue({
      schemaVersion: 'frisket.workbench_plugin_runtime_index.v1',
      projectId: 'test-project',
      arbitraryPackageLoadAllowed: false,
      receiptScanLimit: 50,
      skippedInvalidReceipts: 0,
      skippedInvalidManifestRefs: 0,
      loadedPluginCount: 0,
      firstParty: { schemaVersion: 'frisket.workbench_descriptor_package.v1', descriptors: [] },
      plugins: [],
    });

    render(<ProjectSecretsSettings project={OWNER} />);

    await waitFor(() => expect(getWorkbenchPluginRuntimeIndex).toHaveBeenCalled());
  });
});
