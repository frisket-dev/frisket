import { expect, test } from '@playwright/test';
import { createProject, importCsv, openDiscoverTab, openProject, uniqueName } from './helpers';

// Notifications is a sibling Discover tab, not a nested sub-section stacked
// under Watches. Pins: both tabs present, each panel lives
// under its own tab, Watches shows only watches (no nested NOTIFICATIONS
// header), and the Notifications tab carries its own (BellRing) icon.

test('Notifications is a sibling Discover tab, not nested under Watches', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-discover-notif-tab'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Watches tab: only the watches contribution renders. No nested Notifications
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
  await expect(page.getByTestId('watches-panel')).toHaveCount(0);
});

test('notifications tab reveals from a run notification opened while viewing Notifications', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-discover-notif-open'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'notify.csv',
    'title,status\nBudget hearing,open\n',
  );
  const watchResponse = await page.request.post(`/api/projects/${pid}/watches`, {
    data: {
      name: 'Budget watch',
      scope: { kind: 'project' },
      query: { kind: 'fts', q: 'budget', limit: 10 },
    },
  });
  expect(watchResponse.ok()).toBeTruthy();
  const watch = await watchResponse.json();
  const runResponse = await page.request.post(`/api/projects/${pid}/watches/${watch.id}/run`);
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
  // Watches tab (where the linked run lives) even though it was unmounted
  // while Notifications was active.
  await expect(page.getByTestId('discover-tab-Watches')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('watches-panel')).toBeVisible();
  const watchItem = page.getByTestId('watch-item').filter({ hasText: 'Budget watch' });
  await expect(watchItem.getByTestId('watch-run-history')).toBeVisible();
});
