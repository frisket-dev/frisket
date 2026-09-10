import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  openDiscoverTab,
  openProject,
  uniqueName,
} from './helpers';

// panel-reveal-over-drawer-v1: same genre as the 2026-07-07 modalOrPeek
// z-order regression. Sources & connections now opens in a focused modal,
// which must cover an in-progress Action drawer without destroying its draft.
// Resident Discover panels still close the drawer before revealing a tab;
// the notification case below pins that separate behavior.
//
// watches/notifications-split reconciliation (2026-07-09): the second case
// used to drive this via a `frisket:discover-select-tab` window CustomEvent,
// which NotificationsPanel's "open source" button dispatched. discover-
// intent-slice-v1 retired that event entirely — the button now calls
// chrome.setDiscoverTab/setDiscoverOpen directly (App.tsx's dismissal effect
// already reacts to the resulting discoverTab/discoverOpen state change, not
// to any event, so nothing in production needed rewiring). There is no
// window-level hook left to fire that state change without going through
// the real button, so this retargets to the real trigger: seed a
// notification with a watch_run deep link, open the Notifications tab
// (mounts the button), THEN open the drawer (covers it), and fire a real
// DOM click via dispatchEvent — which bypasses Playwright's actionability/
// visibility checks (unlike .click()) but still invokes the covered
// button's onClick, exactly reproducing "the button is right there but
// painted under the drawer" without needing the retired event shortcut.

async function seed(page: Page): Promise<{ pid: string; sheetId: number }> {
  const pid = await createProject(page.request, uniqueName('e2e-panel-reveal'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  return { pid, sheetId };
}

async function openDrawer(page: Page): Promise<void> {
  await openAction(page, 'map.summarize');
  await expect(page.getByTestId('action-drawer')).toBeVisible();
}

test('Sources & connections (project switcher) reveals above an open Action drawer', async ({ page }) => {
  await seed(page);
  await openDrawer(page);

  await page.getByTestId('switch-project').click();
  await page.getByTestId('project-sources-open').click();

  // The focused manager wins the visual stack while preserving the draft in
  // the drawer underneath it.
  await expect(page.getByTestId('sources-connections-dialog')).toBeVisible();
  await expect(page.getByTestId('sources-connections-close')).toBeFocused();
  await expect(page.getByTestId('action-drawer')).toBeVisible();
  await expect(page.getByTestId('sources-panel')).toBeVisible();
});

test('a notification-run reveal (Watches tab) also closes an open Action drawer', async ({ page }) => {
  const { pid } = await seed(page);

  const notification = {
    id: 501,
    source_kind: 'watch',
    source_ref: { watch_id: 7 },
    source_event_ids: [901],
    event_count: 1,
    event_kinds: ['watch_run_completed'],
    title: 'Watch run completed',
    summary: '1 new result',
    severity: 'info',
    deep_link: { kind: 'watch_run', watch_id: 7, run_id: 55, event_ids: [901] },
    created_at: '2026-07-08T12:00:00Z',
    updated_at: '2026-07-08T12:00:00Z',
    state: 'unseen',
    seen_at: null,
    read_at: null,
    acknowledged_at: null,
    acknowledged_by: null,
  };
  await page.route(`**/api/projects/${pid}/notifications/summary`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.notifications_summary.v1',
        total: 1,
        unseen: 1,
        seen: 0,
        read: 0,
        acknowledged: 0,
        by_severity: { info: 1 },
        by_source_kind: { watch: 1 },
        by_source_ref: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/notifications?*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.notifications_page.v1',
        order: 'desc',
        offset: 0,
        limit: 20,
        total: 1,
        has_more: false,
        next_offset: null,
        notifications: [notification],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/notifications/${notification.id}/read`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        notification_id: notification.id,
        actor_id: 'e2e-actor',
        state: 'read',
        seen_at: null,
        read_at: '2026-07-08T12:05:00Z',
        acknowledged_at: null,
        acknowledged_by: null,
        updated_at: '2026-07-08T12:05:00Z',
      }),
    });
  });

  // Mount the Notifications tab (and its "open source" button) BEFORE the
  // drawer opens and covers it — this is the state discover-intent-slice-v1
  // left in place of the retired window event.
  await openDiscoverTab(page, 'Notifications');
  const openSource = page.getByTestId('notification-open-source');
  await expect(openSource).toBeVisible();

  await openDrawer(page);

  // The button is now painted under the drawer — dispatchEvent fires a real
  // DOM click on it directly, bypassing Playwright's covered-element check
  // the same way a user's click physically cannot.
  await openSource.dispatchEvent('click');

  await expect(page.getByTestId('action-drawer')).toHaveCount(0);
  await expect(page.getByTestId('discover-tab-Watches')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('workbench-contribution-frisket-core-panel-watches')).toBeVisible();
});
