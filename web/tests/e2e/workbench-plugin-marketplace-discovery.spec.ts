import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';
import { expectBundledPluginRows, openPluginManager } from './workbenchPluginLifecycleFixture';

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
  await expectBundledPluginRows(page);
  await expect(page.getByTestId('workbench-plugin-marketplace-discovery')).toHaveCount(0);
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);

  await manager.getByTestId('plugin-manager-install-local').click();
  await expect(manager.getByTestId('plugin-manager-error')).toHaveText(
    'Plugin id and local source path are required.',
  );
});
