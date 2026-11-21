import { expect, test, type Locator, type APIRequestContext } from '@playwright/test';
import { createProject, importCsv, openDiscoverTab, openPalette, openProject, sheetColumns, uniqueName } from './helpers';

async function createSearchAlert(request: APIRequestContext, pid: string, sheetId: number, name: string) {
  const view = await request.post(`/api/projects/${pid}/views`, { data: {
    name, selection: { scope: { kind: 'sheet', sheet_id: sheetId }, search: { text: 'budget' }, filters: [], order: [], limit: null },
    presentation: { columns: null, column_groups: null },
  } });
  expect(view.ok()).toBeTruthy();
  return request.post(`/api/projects/${pid}/alerts`, { data: {
    name, view_id: (await view.json()).id,
    detection_policy: { kind: 'conditions', entered: true, exited: false, count_changed: false, fields: [] },
  } });
}

async function declaredCapabilities(contribution: Locator): Promise<string[]> {
  return (await contribution.getAttribute('data-required-capabilities'))
    ?.split(/\s+/)
    .filter(Boolean) ?? [];
}

function alertWire(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 81,
    name: 'Open rows watch',
    view_id: 81,
    schedule: '@hourly',
    sheet_id: null,
    detection_policy: { kind: 'conditions', entered: true, exited: false, count_changed: false, fields: [] },
    enabled: true,
    last_evaluated_op: 0,
    last_run_id: null,
    last_status: null,
    created_at: '2026-08-31T00:00:00Z',
    updated_at: '2026-08-31T00:00:00Z',
    latest_run: null,
    ...overrides,
  };
}

test('alerts promote searches, then show manual run history', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-watchlists'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'watch.csv',
    'title,status,body\n' +
      'Budget hearing,open,The council discussed budget oversight.\n' +
      'Bridge repair,closed,The bridge repair vote was tabled.\n' +
      'Budget audit,open,Auditors found budget variances.\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const columnId = (name: string) => columns.find(column => column.name === name)!.id;
  const viewResponse = await page.request.post(`/api/projects/${pid}/views`, {
    data: {
      name: 'Open rows',
      selection: { scope: { kind: 'sheet', sheet_id: sheetId },
        filters: [{ column_id: columnId('status'), condition: { eq: 'open' } }],
        order: [{ column_id: columnId('title'), dir: 'asc' }], limit: null },
      presentation: { columns: null, column_groups: null },
    },
  });
  expect(viewResponse.ok()).toBeTruthy();

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Alerts re-homed into the Discover panel's Alerts tab
  // (workbench-ia-right-edge-v1); Notifications is a sibling tab of its own
  // (discover-notifications-tab-v1).
  await openDiscoverTab(page, 'Alerts');

  const alertsContribution = page.getByTestId(
    'workbench-contribution-frisket-core-panel-alerts',
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
  const alertCapabilities = await declaredCapabilities(alertsContribution);
  expect(alertCapabilities).toEqual(expect.arrayContaining([
    'alert.list',
    'alert.check',
    'alert.checks.list',
    'notifications.summary.read',
  ]));
  expect(alertCapabilities).toContain('embedding.index.refresh');

  await openPalette(page);
  await page.getByTestId('command-palette-input').fill('budget');
  await page.getByTestId('search-mode-select').selectOption('lexical');
  await expect(page.getByTestId('search-results')).toBeVisible();

  const searchAlertResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'POST') return false;
    return new URL(res.url()).pathname === `/api/projects/${pid}/alerts`;
  });
  await page.getByTestId('alert-search-button').click();
  const searchAlert = await (await searchAlertResponse).json();
  expect(searchAlert.name).toBe('Search: budget');
  const watchedView = await (await page.request.get(`/api/projects/${pid}/views/${searchAlert.view_id}`)).json();
  expect(watchedView.spec.selection).toMatchObject({ search: { text: 'budget' } });
  await page.keyboard.press('Escape');

  await expect(page.getByTestId('alerts-panel')).toBeVisible();
  const searchAlertItem = page.getByTestId('alert-item').filter({ hasText: 'Search: budget' });
  await expect(searchAlertItem).toBeVisible();

  const runResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'POST') return false;
    return new URL(res.url()).pathname === `/api/projects/${pid}/alerts/${searchAlert.id}/check`;
  });
  await searchAlertItem.getByTestId('check-alert').click();
  const runBody = await (await runResponse).json();
  expect(runBody.schema_version).toBe('frisket.alert_check.v1');
  expect(runBody.run.matched_rows).toBe(2);
  // The first successful check establishes the View's baseline.
  expect(runBody.run.new_rows).toBe(0);
  await expect(searchAlertItem.getByTestId('alert-latest-run')).toContainText('0 new');

  const historyResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/alerts/${searchAlert.id}/checks` &&
      url.searchParams.get('offset') === '0' &&
      url.searchParams.get('limit') === '20'
    );
  });
  await searchAlertItem.getByTestId('toggle-alert-checks').click();
  const historyBody = await (await historyResponse).json();
  expect(historyBody.schema_version).toBe('frisket.alert_checks_page.v1');
  expect(historyBody.runs[0].hits.length).toBeGreaterThan(0);
  await expect(searchAlertItem.getByTestId('alert-check-history')).toContainText('0 new');

});

test('an Alert captures the current grid in a persistent Saved View', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-watch-composer'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'watch-composer.csv',
    'title,status\nBudget hearing,open\nBridge repair,closed\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openDiscoverTab(page, 'Alerts');
  const panel = page.getByTestId('alerts-panel');
  const launch = panel.getByTestId('new-alert-from-current-grid');
  await expect(launch).toHaveText('Create new alert');
  await expect(launch.locator('svg')).toHaveCount(1);
  await launch.click();
  const name = page.getByTestId('alert-create-name');
  const create = page.getByTestId('create-alert-button');
  await expect(page.getByTestId('alert-create-composer')).toContainText(
    'You do not have Slack or email notifications set up. Visit settings to set them up.',
  );
  await expect(page.getByTestId('alert-create-mode')).toHaveCount(0);
  await expect(page.getByTestId('alert-create-source-summary')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Notification settings' })).toHaveCount(0);
  await name.fill('   ');
  await expect(create).toBeDisabled();
  await expect(page.getByTestId('alert-create-name-error')).toHaveText('Enter a name to continue.');

  await name.fill('Daily open rows');
  const createResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/api/projects/${pid}/alerts`
  ));
  await create.click();
  const request = (await createResponse).request().postDataJSON() as Record<string, unknown>;
  expect(request).toMatchObject({
    name: 'Daily open rows',
    view_id: expect.any(Number),
    schedule: '@hourly',
  });
  expect(request).not.toHaveProperty('source_view');
  expect(request).not.toHaveProperty('source_view_id');
  await expect(panel.getByTestId('alert-notification-setup-notice').locator('span')).toHaveText(
    'You do not have Slack or email notifications set up. Visit settings to set them up.',
  );
  await panel.getByLabel('Dismiss notification setup guidance').click();
  await expect(panel.getByTestId('alert-notification-setup-notice')).toHaveCount(0);
  const viewsStore = await (await page.request.get(`/api/projects/${pid}/views`)).json() as Array<{ id: number }>;
  expect(viewsStore.some((view) => view.id === request.view_id)).toBe(true);
});

test('editable Alert controls preserve failed state and describe delete consequences', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-watch-lifecycle'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'watch-lifecycle.csv',
    'title,status\nBudget hearing,open\nBridge repair,closed\n',
  );
  const created = await createSearchAlert(page.request, pid, sheetId, 'Lifecycle watch');
  expect(created.ok()).toBeTruthy();
  const alert = await created.json() as { id: number };
  const patches: Record<string, unknown>[] = [];
  let currentAlert = alertWire({ id: alert.id, name: 'Lifecycle watch', enabled: true });
  let deleteAttempts = 0;
  let releaseRejectedRename = () => {};
  const rejectedRename = new Promise<void>((resolve) => { releaseRejectedRename = resolve; });

  await page.route(`**/api/projects/${pid}/alerts/${alert.id}`, async (route) => {
    const request = route.request();
    if (request.method() === 'PATCH') {
      const patch = request.postDataJSON() as Record<string, unknown>;
      patches.push(patch);
      if (patch.name === 'Rejected name') {
        await rejectedRename;
        await route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: 'name rejected' }) });
        return;
      }
      currentAlert = {
        ...currentAlert,
        ...(typeof patch.name === 'string' ? { name: patch.name } : {}),
        ...(typeof patch.enabled === 'boolean' ? { enabled: patch.enabled } : {}),
      };
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(currentAlert),
      });
      return;
    }
    if (request.method() === 'DELETE') {
      deleteAttempts += 1;
      if (deleteAttempts === 1) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'delete failed' }) });
        return;
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, deleted: alert.id }) });
      return;
    }
    await route.continue();
  });

  await openProject(page, pid, sheetId);
  await openDiscoverTab(page, 'Alerts');
  const item = page.getByTestId('alert-item').filter({ hasText: 'Lifecycle watch' });
  await expect(item).toBeVisible();
  await item.getByTestId('rename-alert').click();
  const rename = item.getByTestId('alert-rename-input');
  const saveRename = item.getByTestId('save-alert-rename');
  await rename.fill('   ');
  await expect(saveRename).toBeDisabled();
  await expect(item.getByTestId('alert-rename-error')).toHaveText('Enter a name to continue.');
  await rename.fill('Renamed lifecycle watch');
  await saveRename.click();
  expect(patches).toContainEqual({ name: 'Renamed lifecycle watch' });

  const renamed = page.getByTestId('alert-item').filter({ hasText: 'Renamed lifecycle watch' });
  await renamed.getByTestId('pause-alert').click();
  expect(patches).toContainEqual({ enabled: false });
  await expect(renamed.getByTestId('alert-status')).toHaveText('Paused');
  await expect(renamed.getByTestId('check-alert')).toHaveAccessibleName(/full fresh check/i);
  await expect(renamed.getByTestId('check-alert')).toBeEnabled();
  await renamed.getByTestId('resume-alert').click();
  expect(patches).toContainEqual({ enabled: true });
  await expect(renamed.getByTestId('alert-status')).toHaveCount(0);

  await renamed.getByTestId('rename-alert').click();
  await renamed.getByTestId('alert-rename-input').fill('Rejected name');
  await renamed.getByTestId('save-alert-rename').click();
  await expect(renamed.getByTestId('save-alert-rename')).toBeDisabled();
  releaseRejectedRename();
  await expect(renamed.getByTestId('alert-mutation-error')).toContainText('name rejected');
  await expect(renamed.getByTestId('alert-rename-input')).toHaveValue('Rejected name');

  await renamed.getByTestId('delete-alert').click();
  const confirmation = page.getByTestId('delete-alert-confirmation');
  await expect(confirmation).toContainText('check history and in-app notifications will be deleted');
  await expect(confirmation).toContainText('already-sent email or external notifications cannot be recalled');
  const cancelDelete = confirmation.getByRole('button', { name: 'Cancel' });
  await expect(cancelDelete).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(confirmation).toHaveCount(0);
  await expect(renamed.getByTestId('delete-alert')).toBeFocused();

  await renamed.getByTestId('delete-alert').click();
  await confirmation.getByTestId('confirm-delete-alert').click();
  await expect(confirmation).toBeVisible();
  await expect(confirmation.getByTestId('alert-mutation-error')).toContainText('delete failed');
  await expect(confirmation.getByTestId('confirm-delete-alert')).toBeEnabled();
  await confirmation.getByTestId('confirm-delete-alert').click();
  await expect(renamed).toHaveCount(0);
  await expect(page.getByTestId('alerts')).toBeFocused();
});

test('Alert lifecycle controls remain usable at Discover\'s constrained width', async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 760 });
  const pid = await createProject(page.request, uniqueName('e2e-watches-narrow'));
  const sheetId = await importCsv(page.request, pid, 'watch-narrow.csv', 'title\nBudget hearing\n');
  const created = await createSearchAlert(page.request, pid, sheetId, 'Narrow lifecycle watch');
  expect(created.ok()).toBeTruthy();
  await page.addInitScript(
    ([projectId]) => localStorage.setItem(`frisket:discover-width:${projectId}`, '220'),
    [pid],
  );

  await openProject(page, pid, sheetId);
  await openDiscoverTab(page, 'Alerts');
  const discoverBox = await page.getByTestId('discover-panel').boundingBox();
  expect(discoverBox?.width).toBeGreaterThanOrEqual(219);
  expect(discoverBox?.width).toBeLessThanOrEqual(221);
  const panel = page.getByTestId('alerts-panel');
  const item = panel.getByTestId('alert-item').filter({ hasText: 'Narrow lifecycle watch' });
  await expect(item).toBeVisible();
  await expect(item.getByTestId('check-alert')).toBeVisible();
  await expect(item.getByTestId('rename-alert')).toBeVisible();
  await expect(item.getByTestId('pause-alert')).toBeVisible();
  await expect(item.getByTestId('delete-alert')).toBeVisible();
  expect(await panel.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);

  await panel.getByTestId('new-alert-from-current-grid').click();
  const composer = panel.getByTestId('alert-create-composer');
  await expect(composer).toBeVisible();
  await expect(composer.getByText('Choose what to alert on.', { exact: true })).toBeVisible();
  await expect(composer.getByText('You do not have Slack or email notifications set up. Visit settings to set them up.')).toBeVisible();
  expect(await composer.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);

  await composer.getByTestId('alert-create-name').fill('Narrow current watch');
  const createResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === `/api/projects/${pid}/alerts`
  ));
  await composer.getByTestId('create-alert-button').click();
  const createdFromGrid = await createResponse;
  expect(createdFromGrid.ok()).toBeTruthy();
  expect(createdFromGrid.request().postDataJSON()).toMatchObject({
    name: 'Narrow current watch',
    view_id: expect.any(Number),
    schedule: '@hourly',
  });
  expect(await createdFromGrid.json()).toMatchObject({ detection_policy: { kind: 'conditions', entered: true } });
  await expect(panel.getByTestId('alert-item').filter({ hasText: 'Narrow current watch' })).toBeVisible();
  const viewsStore = await (await page.request.get(`/api/projects/${pid}/views`)).json() as Array<{ id: number }>;
  expect(viewsStore.some((view) => view.id === createdFromGrid.request().postDataJSON().view_id)).toBe(true);
});

test('an old Watch-list response cannot restore renamed, paused, or deleted Alerts', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-watch-stale-list'));
  const sheetId = await importCsv(page.request, pid, 'watch-stale-list.csv', 'title\nBudget hearing\n');
  const create = async (name: string) => {
    const response = await createSearchAlert(page.request, pid, sheetId, name);
    expect(response.ok()).toBeTruthy();
    return await response.json() as { id: number };
  };
  const renamedAlert = await create('Rename me');
  const pausedAlert = await create('Pause me');
  const deletedAlert = await create('Delete me');

  await openProject(page, pid, sheetId);
  await openDiscoverTab(page, 'Alerts');
  await expect(page.getByTestId('alert-item').filter({ hasText: 'Rename me' })).toBeVisible();

  let releaseStaleList!: () => void;
  const staleListReleased = new Promise<void>((resolve) => { releaseStaleList = resolve; });
  let observeStaleList!: () => void;
  const staleListRequested = new Promise<void>((resolve) => { observeStaleList = resolve; });
  await page.route(`**/api/projects/${pid}/alerts`, async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    observeStaleList();
    await staleListReleased;
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify([
        alertWire({ id: renamedAlert.id, name: 'Rename me' }),
        alertWire({ id: pausedAlert.id, name: 'Pause me' }),
        alertWire({ id: deletedAlert.id, name: 'Delete me' }),
      ]),
    });
  });

  // This is the public notification used by search-based Alert creation; the
  // delayed GET below is an older server snapshot, not a forged mutation error.
  await page.evaluate(() => window.dispatchEvent(new Event('frisket:alerts-changed')));
  await staleListRequested;

  const renameItem = page.getByTestId('alert-item').filter({ hasText: 'Rename me' });
  await renameItem.getByTestId('rename-alert').click();
  await renameItem.getByTestId('alert-rename-input').fill('Renamed while list is stale');
  await renameItem.getByTestId('save-alert-rename').click();
  await expect(page.getByTestId('alert-item').filter({ hasText: 'Renamed while list is stale' })).toBeVisible();

  const pauseItem = page.getByTestId('alert-item').filter({ hasText: 'Pause me' });
  await pauseItem.getByTestId('pause-alert').click();
  await expect(pauseItem.getByTestId('alert-status')).toHaveText('Paused');

  const deleteItem = page.getByTestId('alert-item').filter({ hasText: 'Delete me' });
  await deleteItem.getByTestId('delete-alert').click();
  await page.getByTestId('delete-alert-confirmation').getByTestId('confirm-delete-alert').click();
  await expect(deleteItem).toHaveCount(0);

  releaseStaleList();
  await expect(page.getByTestId('alert-item').filter({ hasText: 'Renamed while list is stale' })).toBeVisible();
  await expect(page.getByTestId('alert-item').filter({ hasText: 'Rename me' })).toHaveCount(0);
  await expect(pauseItem.getByTestId('alert-status')).toHaveText('Paused');
  await expect(page.getByTestId('alert-item').filter({ hasText: 'Delete me' })).toHaveCount(0);
});

test('viewers can inspect Alerts but have no run, refresh, create, or lifecycle mutation controls', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-viewer-watches'));
  const sheetId = await importCsv(page.request, pid, 'viewer-watch.csv', 'title\nBudget hearing\n');
  const created = await createSearchAlert(page.request, pid, sheetId, 'Read-only watch');
  expect(created.ok()).toBeTruthy();
  const projects = await (await page.request.get('/api/projects')).json() as Array<Record<string, unknown>>;
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(projects.map((project) => project.id === pid ? { ...project, role: 'viewer' } : project)),
    });
  });

  await openProject(page, pid, sheetId);
  await openDiscoverTab(page, 'Alerts');
  const item = page.getByTestId('alert-item').filter({ hasText: 'Read-only watch' });
  await expect(item).toBeVisible();
  await expect(item.getByTestId('check-alert')).toHaveCount(0);
  await expect(item.locator('[data-testid^="alert-refresh-index-"]')).toHaveCount(0);
  await expect(item.getByTestId('rename-alert')).toHaveCount(0);
  await expect(item.getByTestId('pause-alert')).toHaveCount(0);
  await expect(item.getByTestId('resume-alert')).toHaveCount(0);
  await expect(item.getByTestId('delete-alert')).toHaveCount(0);
  await expect(page.getByTestId('alert-create-composer')).toHaveCount(0);
});
