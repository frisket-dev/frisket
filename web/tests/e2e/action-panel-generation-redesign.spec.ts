import { expect, test } from '@playwright/test';
import type { Locator } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  importCsv,
  sheetColumns,
  uniqueName,
} from './helpers';

// This spec originally carried four tests. Three were pure ActionForm
// rendering (template source mode, the single-output Save-to combobox +
// overwrite warning, and the Preview-is-a-footer-action shape) and moved to
// tests/component/GeneratedModelRowsForm.test.tsx and GeneratedActionForm.test.tsx
// — mounted directly with catalog-owned forms instead of importing a CSV through a live
// project. What remains is the one test below: it pins consistency ACROSS
// three different component trees (the drawer host, the Act ribbon band, and
// the column header menu) reading the same actSurface catalog snapshot —
// genuine cross-surface choreography an ActionForm-only mount can't express.

async function expectLauncherNotUsable(locator: Locator): Promise<void> {
  await expect
    .poll(async () => {
      if ((await locator.count()) === 0) return true;
      return locator.isDisabled();
    })
    .toBe(true);
}

async function expectAllLaunchersNotUsable(
  container: Locator,
  testIdPrefix: string,
): Promise<void> {
  const launchers = container.locator(`[data-testid^="${testIdPrefix}"]`);
  const launcherCount = await launchers.count();
  for (let index = 0; index < launcherCount; index += 1) {
    await expectLauncherNotUsable(launchers.nth(index));
  }
}

test('one failed catalog snapshot disables drawer, ribbon, and column launchers without static fallback', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-action-panel-catalog-failure'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'catalog-failure.csv',
    'headline,body\n"Council hearing","The hearing was postponed."\n',
  );
  await page.route('**/actions/v1/catalog', async (route) => {
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Action catalog unavailable' }),
    });
  });

  await page.goto(`/p/${pid}/s/${sheetId}/action/map.summarize`);
  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText(/catalog.*(?:unavailable|failed|invalid)/i);
  await expectLauncherNotUsable(drawer.getByTestId('run-button'));
  await expectLauncherNotUsable(drawer.getByTestId('preview-button'));

  // Close the direct-route drawer without reloading the project. The ribbon
  // and per-column applicability menu must consume that same failed actSurface
  // snapshot, so static templates cannot leave another runnable entry point.
  await page.getByTestId('action-drawer-close').click();
  await expect(drawer).toHaveCount(0);
  const ribbonTabs = page
    .getByRole('tablist', { name: 'Action tabs' })
    .getByRole('tab');
  const ribbonTabCount = await ribbonTabs.count();
  expect(ribbonTabCount).toBeGreaterThan(0);
  for (let index = 0; index < ribbonTabCount; index += 1) {
    const tab = ribbonTabs.nth(index);
    await tab.click();
    await expect(tab).toHaveAttribute('aria-selected', 'true');
    await expectAllLaunchersNotUsable(page.getByTestId('act-ribbon-band'), 'ribbon-action-');
  }

  const columns = await sheetColumns(page.request, pid, sheetId);
  await clickHeaderMenu(page, columns, 'body');
  const headerMenu = page.getByTestId('grid-column-header-menu');
  await expect(headerMenu).toBeVisible();
  await expectAllLaunchersNotUsable(headerMenu, 'header-menu-action-');
});
