import { expect, test } from '@playwright/test';
import { mockHostedSettingsShell } from './settingsTestHelpers';

test('Project Plugins renders typed plugin options and saves project-scoped values', async ({ page }) => {
  await mockHostedSettingsShell(page);

  let mode = 'summary';
  await page.route('**/api/projects/alpha/workbench/plugins', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
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
          pluginId: 'demo.env_settings',
          version: '1.0.0',
          installState: 'enabled',
          activation: 'registryManifestRegistered',
          runtimeSource: 'plugin.load_receipt',
          receiptId: 'receipt-demo',
          manifestSha256: 'manifest-sha',
          packageSha256: 'package-sha',
          byteCount: 100,
          source: { kind: 'localPath', value: '/plugins/demo' },
          contributionSummary: [],
          frontendComponentBindings: [],
          requires: { capabilities: [], secrets: [] },
          arbitraryPackageLoadAllowed: false,
          registryActivated: true,
          installStateSchemaVersion: 'frisket.workbench_plugin_install_state.v1',
          disabledReason: null,
        },
      ],
    }),
  }));
  await page.route('**/api/projects/alpha/workbench/plugins/demo.env_settings/settings', async (route) => {
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON() as { values: Record<string, unknown> };
      mode = String(body.values['demo.env_settings.mode']);
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schemaVersion: 'frisket.workbench_plugin_settings.v1',
        projectId: 'alpha',
        pluginId: 'demo.env_settings',
        canMutate: true,
        settings: [
          {
            id: 'demo.env_settings.mode',
            title: 'Mode',
            type: 'enum',
            description: null,
            defaultValue: 'summary',
            effectiveValue: mode,
            source: mode === 'summary' ? 'default' : 'project',
            enum: ['summary', 'detail'],
            min: null,
            max: null,
            readOnly: false,
          },
        ],
      }),
    });
  });

  await page.goto('/p/alpha/settings/project/plugins');

  const options = page.getByTestId('plugin-options-list');
  await expect(options).toContainText('Mode');
  await options.getByTestId('plugin-option-demo.env_settings.mode').selectOption('detail');
  await options.getByTestId('plugin-option-save-demo.env_settings.mode').click();
  await expect.poll(() => mode).toBe('detail');
  await expect(page.getByText('api_key=secret')).toHaveCount(0);
});

test('Project Plugins disables read-only plugin options', async ({ page }) => {
  await mockHostedSettingsShell(page);

  await page.route('**/api/projects/alpha/workbench/plugins', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
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
          pluginId: 'demo.env_settings',
          version: '1.0.0',
          installState: 'enabled',
          activation: 'registryManifestRegistered',
          runtimeSource: 'plugin.load_receipt',
          receiptId: 'receipt-demo',
          manifestSha256: 'manifest-sha',
          packageSha256: 'package-sha',
          byteCount: 100,
          source: { kind: 'localPath', value: '/plugins/demo' },
          contributionSummary: [],
          frontendComponentBindings: [],
          requires: { capabilities: [], secrets: [] },
          arbitraryPackageLoadAllowed: false,
          registryActivated: true,
          installStateSchemaVersion: 'frisket.workbench_plugin_install_state.v1',
          disabledReason: null,
        },
      ],
    }),
  }));
  await page.route('**/api/projects/alpha/workbench/plugins/demo.env_settings/settings', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      schemaVersion: 'frisket.workbench_plugin_settings.v1',
      projectId: 'alpha',
      pluginId: 'demo.env_settings',
      canMutate: false,
      settings: [
        {
          id: 'demo.env_settings.mode',
          title: 'Mode',
          type: 'enum',
          description: null,
          defaultValue: 'summary',
          effectiveValue: 'summary',
          source: 'default',
          enum: ['summary', 'detail'],
          min: null,
          max: null,
          readOnly: true,
        },
      ],
    }),
  }));

  await page.goto('/p/alpha/settings/project/plugins');

  const options = page.getByTestId('plugin-options-list');
  await expect(options.getByTestId('plugin-option-demo.env_settings.mode')).toBeDisabled();
  await expect(options.getByTestId('plugin-option-save-demo.env_settings.mode')).toBeDisabled();
});
