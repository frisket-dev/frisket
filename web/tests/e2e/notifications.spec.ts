import { expect, test, type Locator } from '@playwright/test';
import { createProject, importCsv, openDiscoverTab, openProject, uniqueName } from './helpers';

async function expectDeclaredCapabilities(contribution: Locator, capabilities: string[]) {
  const declared = (await contribution.getAttribute('data-required-capabilities'))
    ?.split(/\s+/)
    .filter(Boolean) ?? [];
  expect(declared).toEqual(expect.arrayContaining(capabilities));
}

test('generic notification feed links alert events and supports read acknowledgement', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-notifications'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'notify.csv',
    'title,status,body\n' +
      'Budget hearing,open,The council discussed budget oversight.\n' +
      'Bridge repair,closed,The bridge repair vote was tabled.\n' +
      'Budget audit,open,Auditors found budget variances.\n',
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
  const run = (await runResponse.json()).run;

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Notifications is its own sibling Discover tab, Alerts a separate one
  // (discover-notifications-tab-v1); each contribution renders under its own
  // tab.
  await openDiscoverTab(page, 'Notifications');

  const notificationsContribution = page.getByTestId(
    'workbench-contribution-frisket-core-panel-notifications',
  );
  await expect(notificationsContribution).toHaveAttribute(
    'data-schema-version',
    'frisket.workbench.panel.v1',
  );
  await expect(notificationsContribution).toHaveAttribute('data-host', 'leftSidebar');
  await expect(notificationsContribution).toHaveAttribute('data-mode', 'panel');
  await expect(notificationsContribution).toHaveAttribute(
    'data-runtime-component-key',
    'core.panels.NotificationsPanel',
  );
  await expectDeclaredCapabilities(notificationsContribution, [
    'notifications.summary.read',
    'notifications.list',
    'notifications.state.update',
    'watch.run.open',
  ]);

  await expect(page.getByTestId('notifications-panel')).toBeVisible();
  await expect(page.getByTestId('notifications-count')).toHaveText('1');

  const item = page.getByTestId('notification-item').filter({ hasText: 'Budget watch' });
  await expect(item).toBeVisible();
  await expect(item.getByTestId('notification-source-ref')).toContainText(`watch_id: ${alert.id}`);
  await expect(item.getByTestId('notification-source-ref')).toContainText(`run_id: ${run.id}`);

  // Alerts is its own sibling tab (discover-notifications-tab-v1); its
  // contribution + panel only render there.
  await openDiscoverTab(page, 'Watches');
  const alertsContribution = page.getByTestId(
    'workbench-contribution-frisket-core-panel-watches',
  );
  await expect(alertsContribution).toHaveAttribute(
    'data-schema-version',
    'frisket.workbench.panel.v1',
  );
  await expect(alertsContribution).toHaveAttribute('data-host', 'leftSidebar');
  await expect(alertsContribution).toHaveAttribute('data-mode', 'panel');
  await expect(alertsContribution).toHaveAttribute(
    'data-runtime-component-key',
    'core.panels.AlertsPanel',
  );
  await expectDeclaredCapabilities(alertsContribution, [
    'watch.list',
    'watch.run',
    'watch.runs.list',
    'notifications.summary.read',
  ]);

  const alertItem = page.getByTestId('alert-item').filter({ hasText: 'Budget watch' });
  await expect(alertItem).toBeVisible();
  await expect(alertItem.getByTestId('alert-notification-count')).toHaveText('1');

  await openDiscoverTab(page, 'Notifications');
  const readResponse = page.waitForResponse((res) => (
    res.request().method() === 'POST' &&
    new URL(res.url()).pathname.match(new RegExp(`/api/projects/${pid}/notifications/\\d+/read$`)) !== null
  ));
  await item.getByTestId('notification-open-source').click();
  expect((await readResponse).ok()).toBeTruthy();

  // Opening the source reveals the Discover panel's Alerts tab (where the
  // linked run lives), even though it was unmounted while Notifications was
  // active (discover-notifications-tab-v1).
  await expect(page.getByTestId('discover-tab-Watches')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('alerts-panel')).toBeVisible();
  await expect(alertItem.getByTestId('alert-check-history')).toContainText('2 new');
  await expect(alertItem.getByTestId('alert-check-events')).toContainText('row_entered');

  await openDiscoverTab(page, 'Notifications');
  await expect(item.getByTestId('notification-state')).toContainText('read');
  await item.getByTestId('notification-ack').click();
  await expect(item.getByTestId('notification-state')).toContainText('acknowledged');
  await item.getByTestId('notification-unack').click();
  await expect(item.getByTestId('notification-state')).toContainText('read');
});
