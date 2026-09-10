import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';
import { openPluginManager } from './workbenchPluginLifecycleFixture';

test('plugin manager exposes explicit local install without marketplace repair path', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-workbench-plugin-manager'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'name\nAda\n');
  await openProject(page, pid, sheetId);

  const manager = await openPluginManager(page);
  await expect(manager).toBeVisible();
  await expect(manager).toHaveAttribute('data-schema-version', 'frisket.workbench_plugin_runtime_index.v1');
  await expect(manager).toHaveAttribute('data-project-id', pid);
  // Pin revision: a fresh project ships with every bundled plugin -- frisket.geo
  // (geo-bundled-plugin-v1, fc603a13), frisket.media (the managed yt-dlp runtime
  // plugin, 48a45b5d), the dormant frisket.ftm importer (ftm-bundled-plugin-v1,
  // auto_enable:false but still installed like the media precedent), and
  // frisket.transliterate (decision 15 bonus, auto_enable:true) -- seeded
  // through the public bundled install path at project creation
  // (bootstrap_project_bundled_plugins walks every package in
  // src/frisket/authoring/bundled_plugins/), matching workbench-plugin-real-local-smoke
  // .spec.ts's data-plugin-count pin. So "explicit local install without
  // marketplace repair path" starts from a count of 4, not an empty runtime index.
  await expect(manager).toHaveAttribute('data-plugin-count', '4');
  await expect(page.getByTestId('workbench-plugin-marketplace-discovery')).toHaveCount(0);
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);

  await manager.getByTestId('plugin-manager-install-local').click();
  await expect(manager.getByTestId('plugin-manager-error')).toHaveText(
    'Plugin id and local source path are required.',
  );
});
