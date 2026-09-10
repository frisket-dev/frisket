import { expect, test } from '@playwright/test';
import { mockHostedSettingsShell } from './settingsTestHelpers';

function runtimeIndex() {
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
      },
    ],
  };
}

test('Project Secrets owns plugin secret requirements and Project Plugins owns lifecycle surface', async ({ page }) => {
  await mockHostedSettingsShell(page);

  let secrets = [
    {
      name: 'WEATHER_API_KEY',
      hint: '...demo',
      configured: true,
      updatedAt: '2026-07-01T12:00:00Z',
      consumers: [{ kind: 'plugin', id: 'weather.forecast' }],
    },
  ];
  const secretsPayload = () => ({
    schemaVersion: 'frisket.project_secrets.v1',
    projectId: 'alpha',
    secrets,
    conflicts: [],
  });

  await page.route('**/api/projects/alpha/secrets', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(secretsPayload()) });
      return;
    }
    const body = route.request().postDataJSON() as { name: string; value: string };
    secrets = [
      ...secrets.filter((item) => item.name !== body.name.toUpperCase()),
      {
        name: body.name.toUpperCase(),
        hint: '...cret',
        configured: true,
        updatedAt: '2026-07-01T12:30:00Z',
        consumers: [],
      },
    ];
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(secretsPayload()) });
  });
  await page.route('**/api/projects/alpha/secrets/*', async (route) => {
    const name = decodeURIComponent(route.request().url().split('/').pop() ?? '');
    secrets = secrets.filter((item) => item.name !== name);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, deleted: true, name }) });
  });
  await page.route('**/api/projects/alpha/workbench/plugins', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(runtimeIndex()),
  }));
  await page.route('**/api/projects/alpha/workbench/plugins/weather.forecast/settings', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      schemaVersion: 'frisket.workbench_plugin_settings.v1',
      projectId: 'alpha',
      pluginId: 'weather.forecast',
      canMutate: true,
      settings: [],
    }),
  }));

  await page.goto('/p/alpha/settings/project/secrets');

  const secretsSection = page.getByTestId('project-secrets-settings');
  await expect(secretsSection).toBeVisible();
  await expect(secretsSection).toContainText('WEATHER_API_KEY');
  await expect(secretsSection).toContainText('weather.forecast');
  // settings-secret-deeplink-v1: visible while typing by default (the table
  // only ever shows the last-4 hint); the toggle flips to password-masking.
  await expect(page.getByLabel('Project secret value')).toHaveAttribute('type', 'text');
  await page.getByTestId('secret-value-visibility').click();
  await expect(page.getByLabel('Project secret value')).toHaveAttribute('type', 'password');
  await page.getByTestId('secret-value-visibility').click();
  await page.getByLabel('Project secret name').fill('new_api_key');
  await page.getByLabel('Project secret value').fill('secret-value');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByText('secret-value')).toHaveCount(0);
  await expect(secretsSection).toContainText('NEW_API_KEY');

  await page.goto('/p/alpha/settings/project/plugins');

  const pluginsSection = page.getByTestId('project-plugins-settings');
  await expect(pluginsSection).toBeVisible();
  // Local-install gating is EDITION-scoped, not identity-scoped: identityMode
  // comes from the immutable module passed by the edition entrypoint, so
  // mockHostedSettingsShell's /api/me route cannot flip it.
  // This suite boots the 'local' edition (playwright.config.ts), where local
  // plugin install is intentionally AVAILABLE — pin that, plus the absence of
  // the hosted-only notice. The hosted branch (allowLocalInstall=false renders
  // plugin-manager-local-install-disabled and no install form) is pinned in
  // tests/component/PluginManagerLocalInstallGate.test.tsx, since it is
  // structurally unreachable under the local harness (see
  // playwright.team.config.ts for the same split).
  await expect(pluginsSection.getByTestId('plugin-manager-install-local')).toBeVisible();
  await expect(pluginsSection.getByTestId('plugin-manager-local-install-disabled')).toHaveCount(0);
  await expect(pluginsSection.getByTestId('plugin-manager-installed-plugin')).toHaveAttribute('data-plugin-id', 'weather.forecast');
  await expect(pluginsSection.getByTestId('plugin-manager-installed-plugin')).toHaveAttribute('data-secrets', 'WEATHER_API_KEY');
  await expect(page.getByTestId('workbench-shell')).toHaveCount(0);
});
