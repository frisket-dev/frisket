import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';

// The Plugins bottom-dock tab retired — it was a
// read-only duplicate of the full manager Settings already hosts. Plugin
// load/runtime FAILURES now route into the Errors dock tab instead
// (dockJobSummary.ts's derivePluginErrorJobs), deep-linking back to
// Settings → Plugins (WorkbenchBottomDock.tsx's openPluginSettingsFromError +
// PluginManager.tsx's ?plugin= highlight, settings-secret-deeplink-v1
// precedent). This spec is the live-UI proof for that routing, and (in the
// second test) that Settings → Plugins is still the full manager with no
// dock tab left to reach it from.

const brokenPluginId = 'frisket.broken_example';

function brokenPluginEntry() {
  return {
    schemaVersion: 'frisket.workbench_plugin_runtime_plugin.v1',
    pluginId: brokenPluginId,
    version: '0.1.0',
    installState: 'failed',
    activation: 'failed',
    runtimeSource: 'plugin.load_receipt',
    receiptId: 'receipt_broken_example',
    manifestSha256: 'sha256:broken-example',
    packageSha256: 'sha256:broken-example-package',
    byteCount: 128,
    source: { kind: 'local_file', path: '/plugins/frisket-broken-example/plugin.json' },
    contributionSummary: [],
    frontendComponentBindings: [],
    requires: { capabilities: [], secrets: [] },
    arbitraryPackageLoadAllowed: false,
    registryActivated: false,
    installStateSchemaVersion: 'frisket.workbench_plugin_install_state.v1',
    disabledReason: null,
    installFailure: {
      code: 'manifest_invalid',
      message: 'workbench-descriptors.json failed schema validation: unknown placement host "sidebarr"',
    },
  };
}

function runtimeIndexBody(pid: string, plugins: ReturnType<typeof brokenPluginEntry>[]) {
  return {
    schemaVersion: 'frisket.workbench_plugin_runtime_index.v1',
    projectId: pid,
    arbitraryPackageLoadAllowed: false,
    receiptScanLimit: 5000,
    skippedInvalidReceipts: 0,
    skippedInvalidManifestRefs: 0,
    loadedPluginCount: plugins.length,
    plugins,
    firstParty: { schemaVersion: 'frisket.workbench_descriptor_package.v1', descriptors: [] },
  };
}

function routeFailedPluginRuntimeIndex(page: Page, pid: string) {
  return page.route(`**/api/projects/${pid}/workbench/plugins`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(runtimeIndexBody(pid, [brokenPluginEntry()])),
    });
  });
}

// Mutable variant for the mid-session reconciliation test below: the SAME
// route handler is re-invoked on every matching request, reading `failed`
// fresh each time — flipping it lets a test simulate a plugin state change
// that happened OUTSIDE this page (another tab, the dev loop, a repair)
// without needing a second page.route() call.
async function routeMutablePluginRuntimeIndex(page: Page, pid: string) {
  let failed = false;
  await page.route(`**/api/projects/${pid}/workbench/plugins`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(runtimeIndexBody(pid, failed ? [brokenPluginEntry()] : [])),
    });
  });
  return { setFailed: (next: boolean) => { failed = next; } };
}

test('a failed plugin surfaces in the Errors dock tab and deep-links to Settings → Plugins', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('plugin-failure-errors'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await routeFailedPluginRuntimeIndex(page, pid);
  await openProject(page, pid, sheetId);

  // No Plugins dock tab exists at all (decision 10) — the failure must
  // reach the user some other way, which the rest of this test proves.
  await expect(page.getByTestId('bottom-dock-tab-plugins')).toHaveCount(0);

  // The Errors tab carries a failure-count badge that includes the plugin
  // failure (tabBadges.errors, App.tsx's WorkspaceBottomDockRegion).
  const errorsBadge = page.getByTestId('bottom-dock-tab-badge-errors');
  await expect(errorsBadge).toBeVisible();
  await expect(errorsBadge).toHaveText('1');

  await page.getByTestId('bottom-dock-tab-errors').click();
  const errorsPanel = page.getByTestId('workbench-contribution-frisket-core-panel-errors');
  await expect(errorsPanel).toBeVisible();

  const row = errorsPanel.locator('[data-job-kind="plugin"]');
  await expect(row).toBeVisible();
  await expect(row).toContainText(`Plugin: ${brokenPluginId}`);
  await expect(row).toContainText('workbench-descriptors.json failed schema validation');
  await row.click();

  const settingsLink = page.getByTestId('bottom-dock-plugin-error-settings-link');
  await expect(settingsLink).toBeVisible();
  await settingsLink.click();

  // The deep link lands on Settings → Plugins (not the retired dock tab),
  // riding the failed plugin's id as ?plugin= so it can be highlighted.
  await expect(page).toHaveURL(
    new RegExp(`/p/${pid}/settings/project/plugins\\?plugin=${brokenPluginId}$`),
  );
  const manager = page.getByTestId('plugin-manager');
  await expect(manager).toBeVisible();
  await expect(manager).toHaveAttribute('data-mode', 'settings');
  const highlighted = manager
    .getByTestId('plugin-manager-installed-plugin')
    .filter({ hasText: brokenPluginId });
  await expect(highlighted).toBeVisible();
  await expect(highlighted).toHaveAttribute('data-highlighted', 'true');
  await expect(highlighted).toHaveAttribute('data-install-state', 'failed');
});

test('Settings → Plugins is the sole manager surface: full lifecycle chrome, no dock tab to reach it from', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('plugin-settings-sole-manager'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await routeFailedPluginRuntimeIndex(page, pid);
  await openProject(page, pid, sheetId);

  await expect(page.getByTestId('bottom-dock-tab-plugins')).toHaveCount(0);

  await page.goto(`/p/${pid}/settings/project/plugins`);
  const manager = page.getByTestId('plugin-manager');
  await expect(manager).toBeVisible();
  await expect(manager).toHaveAttribute('data-mode', 'settings');
  // mode="settings" renders the full lifecycle chrome (install form + row
  // action buttons) that mode="status" (the retired dock tab) hid — the
  // "duplicate read-only surface" decision 10 removed, not the manager itself.
  await expect(page.getByTestId('plugin-manager-install-plugin-id')).toBeVisible();
  await expect(page.getByTestId('plugin-manager-install-source')).toBeVisible();
  const row = manager.getByTestId('plugin-manager-installed-plugin').filter({ hasText: brokenPluginId });
  await expect(row).toBeVisible();
  await expect(row.getByTestId('plugin-manager-uninstall')).toBeVisible();
});

// The retired frisket.core.panel.plugins tab's
// on-select refresh was the only in-session trigger that kept the runtime
// index (and everything derived from it — contributed dock panels AND these
// synthetic Errors rows) from going stale until a full page reload. The fix
// moved that refresh to Errors-tab selection (useWorkspaceModel.tsx's
// selectBottomDockTab) plus a 30s background usePoll. This test proves BOTH
// directions of a mid-session change — a plugin failing, and a failure
// getting repaired — reach the Errors tab without page.reload(); the
// plugin-contract gauntlet's "dock tab mounts... falls back to jobs" test
// (plugin-ui-gauntlet.spec.ts) proves the CONTRIBUTED-panel side of the same
// shared store field the same way.
test('a mid-session plugin failure (and its repair) reach the Errors tab without a page reload', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('plugin-failure-mid-session'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  const runtimeIndex = await routeMutablePluginRuntimeIndex(page, pid);
  await openProject(page, pid, sheetId);

  // Starts healthy: no Errors badge at all.
  await expect(page.getByTestId('bottom-dock-tab-badge-errors')).toHaveCount(0);

  // A plugin fails mid-session — nothing on THIS page caused it (simulating
  // another tab / the dev loop / an external repair attempt gone wrong).
  runtimeIndex.setFailed(true);

  // Selecting Errors is the explicit, immediate refresh trigger the fix
  // restored — no page.reload() anywhere in this test.
  await page.getByTestId('bottom-dock-tab-errors').click();
  const row = page.locator('[data-job-kind="plugin"]');
  await expect(row).toBeVisible();
  await expect(row).toContainText(`Plugin: ${brokenPluginId}`);
  await expect(page.getByTestId('bottom-dock-tab-badge-errors')).toHaveText('1');

  // The repair direction: an over-reported failure must clear the same way,
  // not stay stuck until a reload.
  runtimeIndex.setFailed(false);
  await page.getByTestId('bottom-dock-tab-errors').click();
  await expect(row).toHaveCount(0);
  await expect(page.getByTestId('bottom-dock-tab-badge-errors')).toHaveCount(0);
});
