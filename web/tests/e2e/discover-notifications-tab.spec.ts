import { expect, test } from '@playwright/test';
import { createProject, importCsv, openDiscoverTab, openProject, uniqueName } from './helpers';

// Notifications is a sibling Discover tab, not a nested sub-section stacked
// under Alerts. Pins: both tabs present, each panel lives
// under its own tab, Alerts shows only alerts (no nested NOTIFICATIONS
// header), and the Notifications tab carries its own (BellRing) icon.

test('Notifications is a sibling Discover tab, not nested under Alerts', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-discover-notif-tab'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Alerts tab: only the alerts contribution renders. No nested Notifications
  // section (neither the old static header nor the panel body) leaks in.
  // (openDiscoverTab reaches the tab whether it's directly visible or folded
  // into the `»` overflow menu, and pins aria-selected — proof both tabs
  // exist as reachable siblings in the strip.)
  await openDiscoverTab(page, 'Alerts');
  await expect(page.getByTestId('workbench-contribution-frisket-core-panel-alerts')).toBeVisible();
  await expect(
    page.getByTestId('workbench-contribution-frisket-core-panel-notifications'),
  ).toHaveCount(0);
  await expect(page.getByTestId('notifications-panel')).toHaveCount(0);
  await expect(page.getByTestId('notifications')).toHaveCount(0);

  // Notifications tab: only the notifications contribution renders, as the
  // tab's own namesake panel (no nested static header of its own).
  await openDiscoverTab(page, 'Notifications');
  await expect(
    page.getByTestId('workbench-contribution-frisket-core-panel-notifications'),
  ).toBeVisible();
  await expect(page.getByTestId('notifications-panel')).toBeVisible();
  await expect(page.getByTestId('workbench-contribution-frisket-core-panel-alerts')).toHaveCount(0);
  await expect(page.getByTestId('alerts-panel')).toHaveCount(0);
});
