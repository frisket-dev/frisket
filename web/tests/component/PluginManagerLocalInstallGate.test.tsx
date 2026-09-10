// @vitest-environment jsdom
//
// PluginManager's allowLocalInstall gate. The hosted branch
// (allowLocalInstall=false in settings mode: render the
// plugin-manager-local-install-disabled notice, no install form) is
// STRUCTURALLY UNREACHABLE under the local Playwright harness —
// ProjectPluginsSettings passes allowLocalInstall={!identityMode}, and
// identityMode comes from the immutable module passed by the edition entry,
// which the local e2e stack mounts as 'local'.
// settings-project-secrets-plugins.spec.ts pins the local
// branch (install form present); this component test pins the hosted branch
// and the status-mode case where neither surface renders.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { PluginManager } from '../../src/workbench/PluginManager';
import type { WorkbenchPluginRuntimeIndex } from '../../src/api/types';

afterEach(cleanup);

function runtimeIndex(
  pluginOverrides: Partial<WorkbenchPluginRuntimeIndex['plugins'][number]> = {},
): WorkbenchPluginRuntimeIndex {
  return {
    schemaVersion: 'frisket.workbench_plugin_runtime_index.v1',
    projectId: 'alpha',
    arbitraryPackageLoadAllowed: false,
    receiptScanLimit: 50,
    skippedInvalidReceipts: 0,
    skippedInvalidManifestRefs: 0,
    loadedPluginCount: 1,
    firstParty: { schemaVersion: 'frisket.workbench_descriptor_package.v1', descriptors: [] },
    plugins: [
      {
        schemaVersion: 'frisket.workbench_plugin_runtime_plugin.v1',
        pluginId: 'weather.forecast',
        version: '1.0.0',
        installState: 'enabled',
        activation: 'registryManifestRegistered',
        runtimeSource: 'plugin.load_receipt',
        receiptId: 'receipt-weather',
        manifestSha256: 'manifest-sha',
        packageSha256: 'package-sha',
        byteCount: 100,
        source: { kind: 'localPath', value: '/plugins/weather' },
        contributionSummary: [{ kind: 'action', ids: ['weather.forecast.lookup'], count: 1 }],
        frontendComponentBindings: [],
        requires: { capabilities: ['network.http'], secrets: ['WEATHER_API_KEY'] },
        settings: [],
        arbitraryPackageLoadAllowed: false,
        registryActivated: true,
        installStateSchemaVersion: 'frisket.workbench_plugin_install_state.v1',
        disabledReason: null,
        ...pluginOverrides,
      },
    ],
  };
}

function renderManager(props: Partial<Parameters<typeof PluginManager>[0]> = {}) {
  return render(
    <PluginManager
      runtimeIndex={runtimeIndex()}
      installLocalWorkbenchPlugin={vi.fn()}
      activateWorkbenchPlugin={vi.fn()}
      activateWorkbenchPluginBackend={vi.fn()}
      disableWorkbenchPlugin={vi.fn()}
      uninstallWorkbenchPlugin={vi.fn()}
      onRefresh={vi.fn()}
      {...props}
    />,
  );
}

describe('PluginManager local-install gate', () => {
  it('hosted settings (allowLocalInstall=false) shows the disabled notice and no install form', () => {
    renderManager({ mode: 'settings', allowLocalInstall: false, canMutate: true });
    expect(screen.getByTestId('plugin-manager-local-install-disabled')).toHaveTextContent(
      'Local plugin install is unavailable in hosted workspaces',
    );
    expect(screen.queryByTestId('plugin-manager-install-local')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plugin-manager-reload')).not.toBeInTheDocument();
    // Lifecycle controls that do not depend on local install stay available.
    expect(screen.getByTestId('plugin-manager-enable')).toBeInTheDocument();
  });

  it('local settings (allowLocalInstall=true) shows the install form and no notice', () => {
    renderManager({ mode: 'settings', allowLocalInstall: true, canMutate: true });
    expect(screen.getByTestId('plugin-manager-install-local')).toBeInTheDocument();
    expect(screen.queryByTestId('plugin-manager-local-install-disabled')).not.toBeInTheDocument();
  });

  it('explains the server-local install contract instead of presenting naked inputs', () => {
    renderManager({ mode: 'settings', allowLocalInstall: true, canMutate: true });

    expect(screen.getByRole('heading', { name: 'Install a local plugin' })).toBeInTheDocument();
    expect(screen.getByLabelText('Plugin folder or manifest path')).toHaveAttribute(
      'placeholder',
      '/absolute/path/to/plugin',
    );
    expect(screen.getByText(/absolute server-local path/i)).toBeInTheDocument();
    expect(screen.getByText(/Browser uploads are not supported/i)).toBeInTheDocument();
    expect(screen.getByLabelText('Manifest plugin ID')).toHaveAttribute(
      'placeholder',
      'example.plugin',
    );
    expect(screen.getByText(/Copy the exact id from plugin.json/i)).toBeInTheDocument();
    expect(screen.getByTestId('plugin-manager-install-local')).toHaveTextContent(
      'Review and install',
    );
  });

  it('status mode renders neither the install form nor the notice', () => {
    renderManager({ mode: 'status', allowLocalInstall: false });
    expect(screen.queryByTestId('plugin-manager-install-local')).not.toBeInTheDocument();
    expect(screen.queryByTestId('plugin-manager-local-install-disabled')).not.toBeInTheDocument();
  });
});

describe('PluginManager lifecycle affordances', () => {
  it('renders state, contributions, access, and integrity as readable plugin details', () => {
    renderManager();

    const badge = screen.getByTestId('plugin-manager-state-badge');
    expect(badge).toHaveAttribute('data-state', 'enabled');
    expect(badge).toHaveClass('plugin-manager-state-badge-positive');
    expect(badge).toHaveTextContent('Enabled');
    expect(screen.getByText(/Available to this project/i)).toBeInTheDocument();
    expect(screen.getByTestId('plugin-manager-contributions')).toHaveTextContent('Actions');
    expect(screen.getByTestId('plugin-manager-contributions')).toHaveTextContent(
      'weather.forecast.lookup',
    );
    expect(screen.getByTestId('plugin-manager-readable-capabilities')).toHaveTextContent(
      'network.http',
    );
    expect(screen.getByTestId('plugin-manager-readable-secrets')).toHaveTextContent(
      'WEATHER_API_KEY',
    );
    expect(screen.getByText('/plugins/weather')).toBeInTheDocument();
    expect(screen.getByText('manifest-sha')).toBeInTheDocument();
    expect(screen.getByText('package-sha')).toBeInTheDocument();
  });

  it('makes a failed plugin visually distinct and shows its runtime failure', () => {
    renderManager({
      runtimeIndex: runtimeIndex({
        installState: 'failed',
        installFailure: { code: 'invalid_plugin_manifest', message: 'Manifest id did not match.' },
      }),
    });

    expect(screen.getByTestId('plugin-manager-state-badge')).toHaveClass(
      'plugin-manager-state-badge-negative',
    );
    expect(screen.getByTestId('plugin-manager-state-badge')).toHaveTextContent('Failed');
    expect(screen.getByRole('alert')).toHaveTextContent('invalid_plugin_manifest');
    expect(screen.getByRole('alert')).toHaveTextContent('Manifest id did not match.');
  });

  it('Enable is disabled (not clickable-looking) once a plugin is already enabled', () => {
    render(
      <PluginManager
        runtimeIndex={runtimeIndex({ installState: 'enabled' })}
        installLocalWorkbenchPlugin={vi.fn()}
        activateWorkbenchPlugin={vi.fn()}
        activateWorkbenchPluginBackend={vi.fn()}
        disableWorkbenchPlugin={vi.fn()}
        uninstallWorkbenchPlugin={vi.fn()}
        onRefresh={vi.fn()}
        mode="settings"
      />,
    );
    const enable = screen.getByTestId('plugin-manager-enable');
    expect(enable).toBeDisabled();
    expect(enable).toHaveAttribute('title', 'Already enabled');
  });

  it('Enable stays clickable for an installed-but-not-yet-enabled plugin', () => {
    render(
      <PluginManager
        runtimeIndex={runtimeIndex({ installState: 'installed' })}
        installLocalWorkbenchPlugin={vi.fn()}
        activateWorkbenchPlugin={vi.fn()}
        activateWorkbenchPluginBackend={vi.fn()}
        disableWorkbenchPlugin={vi.fn()}
        uninstallWorkbenchPlugin={vi.fn()}
        onRefresh={vi.fn()}
        mode="settings"
      />,
    );
    const enable = screen.getByTestId('plugin-manager-enable');
    expect(enable).toBeEnabled();
    expect(enable).not.toHaveAttribute('title', 'Already enabled');
  });

  it('the Backend button explains what activating a backend does', () => {
    render(
      <PluginManager
        runtimeIndex={runtimeIndex({ installState: 'enabled' })}
        installLocalWorkbenchPlugin={vi.fn()}
        activateWorkbenchPlugin={vi.fn()}
        activateWorkbenchPluginBackend={vi.fn()}
        disableWorkbenchPlugin={vi.fn()}
        uninstallWorkbenchPlugin={vi.fn()}
        onRefresh={vi.fn()}
        mode="settings"
      />,
    );
    expect(screen.getByTestId('plugin-manager-activate-backend')).toHaveAttribute(
      'title',
      'Start the trusted local subprocess that registers executable recipes and job handlers.',
    );
  });

  it('uses self-explanatory action labels and tooltips', () => {
    renderManager();

    expect(screen.getByTestId('plugin-manager-reload')).toHaveTextContent('Reload files');
    expect(screen.getByTestId('plugin-manager-reload')).toHaveAttribute(
      'title',
      "Re-read this plugin's manifest and package from its local path, updating the workspace package.",
    );
    expect(screen.getByTestId('plugin-manager-enable')).toHaveTextContent('Review and enable');
    expect(screen.getByTestId('plugin-manager-activate-backend')).toHaveTextContent(
      'Start backend',
    );
    expect(screen.getByTestId('plugin-manager-disable')).toHaveAttribute(
      'title',
      "Stop this plugin's contributions without removing its installation record.",
    );
    expect(screen.getByTestId('plugin-manager-uninstall')).toHaveAttribute(
      'title',
      "Remove this plugin's package from the whole workspace. To turn it off for just this project, use Disable. Source files on disk are not deleted.",
    );
    expect(screen.getByText('What do these actions do?')).toBeInTheDocument();
  });
});
