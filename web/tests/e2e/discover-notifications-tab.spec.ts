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
  await openDiscoverTab(page, 'Watches');
  await expect(page.getByTestId('workbench-contribution-frisket-core-panel-watches')).toBeVisible();
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
  await expect(page.getByTestId('workbench-contribution-frisket-core-panel-watches')).toHaveCount(0);
  await expect(page.getByTestId('alerts-panel')).toHaveCount(0);
});

test('notifications tab reveals from a run notification opened while viewing Notifications', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-discover-notif-open'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'notify.csv',
    'title,status\nBudget hearing,open\n',
  );
  const alertResponse = await page.request.post(`/api/projects/${pid}/watches`, {
    data: {
      name: 'Budget watch',
      scope: { kind: 'project' },
      query: { kind: 'fts', q: 'budget', limit: 10 },
    },
  });
  expect(alertResponse.ok()).toBeTruthy();
  const alert = await alertResponse.json();
  const runResponse = await page.request.post(`/api/projects/${pid}/watches/${alert.id}/run`);
  expect(runResponse.ok()).toBeTruthy();

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await openDiscoverTab(page, 'Notifications');
  const item = page.getByTestId('notification-item').filter({ hasText: 'Budget watch' });
  await expect(item).toBeVisible();

  const readResponse = page.waitForResponse((res) => (
    res.request().method() === 'POST' &&
    new URL(res.url()).pathname.match(new RegExp(`/api/projects/${pid}/notifications/\\d+/read$`)) !== null
  ));
  await item.getByTestId('notification-open-source').click();
  expect((await readResponse).ok()).toBeTruthy();

  // Opening the source from a notification reveals the Discover panel's
  // Alerts tab (where the linked run lives) even though it was unmounted
  // while Notifications was active.
  await expect(page.getByTestId('discover-tab-Watches')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('alerts-panel')).toBeVisible();
  const alertItem = page.getByTestId('alert-item').filter({ hasText: 'Budget watch' });
  await expect(alertItem.getByTestId('alert-check-history')).toBeVisible();
});
