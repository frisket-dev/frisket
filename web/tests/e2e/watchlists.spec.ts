import { expect, test, type Locator } from '@playwright/test';
import { createProject, importCsv, openDiscoverTab, openPalette, openProject, uniqueName } from './helpers';

async function declaredCapabilities(contribution: Locator): Promise<string[]> {
  return (await contribution.getAttribute('data-required-capabilities'))
    ?.split(/\s+/)
    .filter(Boolean) ?? [];
}

function watchWire(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 81,
    name: 'Open rows watch',
    scope: 'project',
    sheet_id: null,
    query: { kind: 'filter' },
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

test('watchlists promote searches, then show manual run history', async ({ page }) => {
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
  const viewResponse = await page.request.post(`/api/projects/${pid}/views`, {
    data: {
      name: 'Open rows',
      sheet_id: sheetId,
      filter: { status: { eq: 'open' } },
      sort: [{ column: 'title', dir: 'asc' }],
    },
  });
  expect(viewResponse.ok()).toBeTruthy();

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Watches re-homed into the Discover panel's Watches tab
  // (workbench-ia-right-edge-v1); Notifications is a sibling tab of its own
  // (discover-notifications-tab-v1).
  await openDiscoverTab(page, 'Watches');

  const watchesContribution = page.getByTestId(
    'workbench-contribution-frisket-core-panel-watches',
  );
  await expect(watchesContribution).toHaveAttribute(
    'data-schema-version',
    'frisket.workbench.panel.v1',
  );
  await expect(watchesContribution).toHaveAttribute('data-host', 'leftSidebar');
  await expect(watchesContribution).toHaveAttribute('data-mode', 'panel');
  await expect(watchesContribution).toHaveAttribute(
    'data-runtime-component-key',
    'core.panels.WatchesPanel',
  );
  const watchCapabilities = await declaredCapabilities(watchesContribution);
  expect(watchCapabilities).toEqual(expect.arrayContaining([
    'watch.list',
    'watch.run',
    'watch.runs.list',
    'notifications.summary.read',
  ]));
  expect(watchCapabilities).toContain('embedding.index.refresh');

  await openPalette(page);
  await page.getByTestId('command-palette-input').fill('budget');
  await expect(page.getByTestId('search-results')).toBeVisible();

  const searchWatchResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'POST') return false;
    return new URL(res.url()).pathname === `/api/projects/${pid}/watches`;
  });
  await page.getByTestId('watch-search-button').click();
  const searchWatch = await (await searchWatchResponse).json();
  expect(searchWatch.name).toBe('Search: budget');
  expect(searchWatch.query).toMatchObject({
    kind: 'fts',
    q: 'budget',
    mode: 'keyword',
    rerank: 'off',
  });
  await page.keyboard.press('Escape');

  await expect(page.getByTestId('watches-panel')).toBeVisible();
  const searchWatchItem = page.getByTestId('watch-item').filter({ hasText: 'Search: budget' });
  await expect(searchWatchItem).toBeVisible();

  const runResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'POST') return false;
    return new URL(res.url()).pathname === `/api/projects/${pid}/watches/${searchWatch.id}/run`;
  });
  await searchWatchItem.getByTestId('run-watch').click();
  const runBody = await (await runResponse).json();
  expect(runBody.schema_version).toBe('frisket.watch_run.v1');
  expect(runBody.run.matched_rows).toBe(2);
  expect(runBody.run.new_rows).toBe(2);
  await expect(searchWatchItem.getByTestId('watch-latest-run')).toContainText('2 new');

  const historyResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/watches/${searchWatch.id}/runs` &&
      url.searchParams.get('offset') === '0' &&
      url.searchParams.get('limit') === '20'
    );
  });
  await searchWatchItem.getByTestId('toggle-watch-runs').click();
  const historyBody = await (await historyResponse).json();
  expect(historyBody.schema_version).toBe('frisket.watch_runs_page.v1');
  expect(historyBody.runs[0].hits.length).toBeGreaterThan(0);
  await expect(searchWatchItem.getByTestId('watch-run-history')).toContainText('2 new');

});

test('a Watch captures the current grid directly without creating a Saved View', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-watch-composer'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'watch-composer.csv',
    'title,status\nBudget hearing,open\nBridge repair,closed\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openDiscoverTab(page, 'Watches');
  const panel = page.getByTestId('watches-panel');
  const launch = panel.getByTestId('new-watch-from-current-grid');
  await expect(launch).toHaveText('Create new watch');
  await expect(launch.locator('svg')).toHaveCount(1);
  await launch.click();
  const name = page.getByTestId('watch-create-name');
  const create = page.getByTestId('create-watch-button');
  await expect(page.getByTestId('watch-create-composer')).toContainText(
    'You do not have Slack or email notifications set up. Visit settings to set them up.',
  );
  await expect(page.getByTestId('watch-create-mode')).toHaveCount(0);
  await expect(page.getByTestId('watch-create-source-summary')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Notification settings' })).toHaveCount(0);
  await name.fill('   ');
  await expect(create).toBeDisabled();
  await expect(page.getByTestId('watch-create-name-error')).toHaveText('Enter a name to continue.');

  await name.fill('Daily open rows');
  const createResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/api/projects/${pid}/watches`
  ));
  await create.click();
  const request = (await createResponse).request().postDataJSON() as Record<string, unknown>;
  expect(request).toMatchObject({
    name: 'Daily open rows',
    query: { kind: 'filter', sheet_id: Number(sheetId), filter: {} },
    scope: { kind: 'sheet', sheet_id: Number(sheetId) },
  });
  expect(request).not.toHaveProperty('source_view');
  expect(request).not.toHaveProperty('source_view_id');
  await expect(panel.getByTestId('watch-notification-setup-notice').locator('span')).toHaveText(
    'You do not have Slack or email notifications set up. Visit settings to set them up.',
  );
  await panel.getByLabel('Dismiss notification setup guidance').click();
  await expect(panel.getByTestId('watch-notification-setup-notice')).toHaveCount(0);
  const savedViews = await (await page.request.get(`/api/projects/${pid}/views`)).json() as Array<{ name: string }>;
  expect(savedViews.some((view) => view.name === 'Daily open rows')).toBe(false);
});

test('editable Watch controls preserve failed state and describe delete consequences', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-watch-lifecycle'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'watch-lifecycle.csv',
    'title,status\nBudget hearing,open\nBridge repair,closed\n',
  );
  const created = await page.request.post(`/api/projects/${pid}/watches`, {
    data: { name: 'Lifecycle watch', query: { kind: 'fts', q: 'budget', mode: 'keyword', rerank: 'off' } },
  });
  expect(created.ok()).toBeTruthy();
  const watch = await created.json() as { id: number };
  const patches: Record<string, unknown>[] = [];
  let currentWatch = watchWire({ id: watch.id, name: 'Lifecycle watch', enabled: true });
  let deleteAttempts = 0;
  let releaseRejectedRename = () => {};
  const rejectedRename = new Promise<void>((resolve) => { releaseRejectedRename = resolve; });

  await page.route(`**/api/projects/${pid}/watches/${watch.id}`, async (route) => {
    const request = route.request();
    if (request.method() === 'PATCH') {
      const patch = request.postDataJSON() as Record<string, unknown>;
      patches.push(patch);
      if (patch.name === 'Rejected name') {
        await rejectedRename;
        await route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: 'name rejected' }) });
        return;
      }
      currentWatch = {
        ...currentWatch,
        ...(typeof patch.name === 'string' ? { name: patch.name } : {}),
        ...(typeof patch.enabled === 'boolean' ? { enabled: patch.enabled } : {}),
      };
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(currentWatch),
      });
      return;
    }
    if (request.method() === 'DELETE') {
      deleteAttempts += 1;
      if (deleteAttempts === 1) {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'delete failed' }) });
        return;
      }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, deleted: watch.id }) });
      return;
    }
    await route.continue();
  });

  await openProject(page, pid, sheetId);
  await openDiscoverTab(page, 'Watches');
  const item = page.getByTestId('watch-item').filter({ hasText: 'Lifecycle watch' });
  await expect(item).toBeVisible();
  await item.getByTestId('rename-watch').click();
  const rename = item.getByTestId('watch-rename-input');
  const saveRename = item.getByTestId('save-watch-rename');
  await rename.fill('   ');
  await expect(saveRename).toBeDisabled();
  await expect(item.getByTestId('watch-rename-error')).toHaveText('Enter a name to continue.');
  await rename.fill('Renamed lifecycle watch');
  await saveRename.click();
  expect(patches).toContainEqual({ name: 'Renamed lifecycle watch' });

  const renamed = page.getByTestId('watch-item').filter({ hasText: 'Renamed lifecycle watch' });
  await renamed.getByTestId('pause-watch').click();
  expect(patches).toContainEqual({ enabled: false });
  await expect(renamed.getByTestId('watch-status')).toHaveText('Paused');
  await expect(renamed.getByTestId('run-watch')).toHaveAccessibleName(/manual evaluation/i);
  await expect(renamed.getByTestId('run-watch')).toBeEnabled();
  await renamed.getByTestId('resume-watch').click();
  expect(patches).toContainEqual({ enabled: true });
  await expect(renamed.getByTestId('watch-status')).toHaveCount(0);

  await renamed.getByTestId('rename-watch').click();
  await renamed.getByTestId('watch-rename-input').fill('Rejected name');
  await renamed.getByTestId('save-watch-rename').click();
  await expect(renamed.getByTestId('save-watch-rename')).toBeDisabled();
  releaseRejectedRename();
  await expect(renamed.getByTestId('watch-mutation-error')).toContainText('name rejected');
  await expect(renamed.getByTestId('watch-rename-input')).toHaveValue('Rejected name');

  await renamed.getByTestId('delete-watch').click();
  const confirmation = page.getByTestId('delete-watch-confirmation');
  await expect(confirmation).toContainText('run history and in-app notifications will be deleted');
  await expect(confirmation).toContainText('already-sent email or external notifications cannot be recalled');
  const cancelDelete = confirmation.getByRole('button', { name: 'Cancel' });
  await expect(cancelDelete).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(confirmation).toHaveCount(0);
  await expect(renamed.getByTestId('delete-watch')).toBeFocused();

  await renamed.getByTestId('delete-watch').click();
  await confirmation.getByTestId('confirm-delete-watch').click();
  await expect(confirmation).toBeVisible();
  await expect(confirmation.getByTestId('watch-mutation-error')).toContainText('delete failed');
  await expect(confirmation.getByTestId('confirm-delete-watch')).toBeEnabled();
  await confirmation.getByTestId('confirm-delete-watch').click();
  await expect(renamed).toHaveCount(0);
  await expect(page.getByTestId('watches')).toBeFocused();
});

test('Watch lifecycle controls remain usable at Discover\'s constrained width', async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 760 });
  const pid = await createProject(page.request, uniqueName('e2e-watches-narrow'));
  const sheetId = await importCsv(page.request, pid, 'watch-narrow.csv', 'title\nBudget hearing\n');
  const created = await page.request.post(`/api/projects/${pid}/watches`, {
    data: { name: 'Narrow lifecycle watch', query: { kind: 'fts', q: 'budget', mode: 'keyword', rerank: 'off' } },
  });
  expect(created.ok()).toBeTruthy();
  await page.addInitScript(
    ([projectId]) => localStorage.setItem(`frisket:discover-width:${projectId}`, '220'),
    [pid],
  );

  await openProject(page, pid, sheetId);
  await openDiscoverTab(page, 'Watches');
  const discoverBox = await page.getByTestId('discover-panel').boundingBox();
  expect(discoverBox?.width).toBeGreaterThanOrEqual(219);
  expect(discoverBox?.width).toBeLessThanOrEqual(221);
  const panel = page.getByTestId('watches-panel');
  const item = panel.getByTestId('watch-item').filter({ hasText: 'Narrow lifecycle watch' });
  await expect(item).toBeVisible();
  await expect(item.getByTestId('run-watch')).toBeVisible();
  await expect(item.getByTestId('rename-watch')).toBeVisible();
  await expect(item.getByTestId('pause-watch')).toBeVisible();
  await expect(item.getByTestId('delete-watch')).toBeVisible();
  expect(await panel.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);

  await panel.getByTestId('new-watch-from-current-grid').click();
  const composer = panel.getByTestId('watch-create-composer');
  await expect(composer).toBeVisible();
  await expect(composer.getByText('Keep an eye on new matches.', { exact: true })).toBeVisible();
  await expect(composer.getByText('You do not have Slack or email notifications set up. Visit settings to set them up.')).toBeVisible();
  expect(await composer.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);

  await composer.getByTestId('watch-create-name').fill('Narrow current watch');
  const createResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === `/api/projects/${pid}/watches`
  ));
  await composer.getByTestId('create-watch-button').click();
  const createdFromGrid = await createResponse;
  expect(createdFromGrid.ok()).toBeTruthy();
  expect(createdFromGrid.request().postDataJSON()).toMatchObject({
    name: 'Narrow current watch',
    query: {
      kind: 'filter',
      sheet_id: Number(sheetId),
      filter: {},
    },
    scope: { kind: 'sheet', sheet_id: Number(sheetId) },
  });
  await expect(panel.getByTestId('watch-item').filter({ hasText: 'Narrow current watch' })).toBeVisible();
  const savedViews = await (await page.request.get(`/api/projects/${pid}/views`)).json() as Array<{ name: string }>;
  expect(savedViews.some((view) => view.name === 'Narrow current watch')).toBe(false);
});

test('an old Watch-list response cannot restore renamed, paused, or deleted Watches', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-watch-stale-list'));
  const sheetId = await importCsv(page.request, pid, 'watch-stale-list.csv', 'title\nBudget hearing\n');
  const create = async (name: string) => {
    const response = await page.request.post(`/api/projects/${pid}/watches`, {
      data: { name, query: { kind: 'fts', q: 'budget', mode: 'keyword', rerank: 'off' } },
    });
    expect(response.ok()).toBeTruthy();
    return await response.json() as { id: number };
  };
  const renamedWatch = await create('Rename me');
  const pausedWatch = await create('Pause me');
  const deletedWatch = await create('Delete me');

  await openProject(page, pid, sheetId);
  await openDiscoverTab(page, 'Watches');
  await expect(page.getByTestId('watch-item').filter({ hasText: 'Rename me' })).toBeVisible();

  let releaseStaleList!: () => void;
  const staleListReleased = new Promise<void>((resolve) => { releaseStaleList = resolve; });
  let observeStaleList!: () => void;
  const staleListRequested = new Promise<void>((resolve) => { observeStaleList = resolve; });
  await page.route(`**/api/projects/${pid}/watches`, async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    observeStaleList();
    await staleListReleased;
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify([
        watchWire({ id: renamedWatch.id, name: 'Rename me' }),
        watchWire({ id: pausedWatch.id, name: 'Pause me' }),
        watchWire({ id: deletedWatch.id, name: 'Delete me' }),
      ]),
    });
  });

  // This is the public notification used by search-based Watch creation; the
  // delayed GET below is an older server snapshot, not a forged mutation error.
  await page.evaluate(() => window.dispatchEvent(new Event('frisket:watches-changed')));
  await staleListRequested;

  const renameItem = page.getByTestId('watch-item').filter({ hasText: 'Rename me' });
  await renameItem.getByTestId('rename-watch').click();
  await renameItem.getByTestId('watch-rename-input').fill('Renamed while list is stale');
  await renameItem.getByTestId('save-watch-rename').click();
  await expect(page.getByTestId('watch-item').filter({ hasText: 'Renamed while list is stale' })).toBeVisible();

  const pauseItem = page.getByTestId('watch-item').filter({ hasText: 'Pause me' });
  await pauseItem.getByTestId('pause-watch').click();
  await expect(pauseItem.getByTestId('watch-status')).toHaveText('Paused');

  const deleteItem = page.getByTestId('watch-item').filter({ hasText: 'Delete me' });
  await deleteItem.getByTestId('delete-watch').click();
  await page.getByTestId('delete-watch-confirmation').getByTestId('confirm-delete-watch').click();
  await expect(deleteItem).toHaveCount(0);

  releaseStaleList();
  await expect(page.getByTestId('watch-item').filter({ hasText: 'Renamed while list is stale' })).toBeVisible();
  await expect(page.getByTestId('watch-item').filter({ hasText: 'Rename me' })).toHaveCount(0);
  await expect(pauseItem.getByTestId('watch-status')).toHaveText('Paused');
  await expect(page.getByTestId('watch-item').filter({ hasText: 'Delete me' })).toHaveCount(0);
});

test('viewers can inspect Watches but have no run, refresh, create, or lifecycle mutation controls', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-viewer-watches'));
  const sheetId = await importCsv(page.request, pid, 'viewer-watch.csv', 'title\nBudget hearing\n');
  const created = await page.request.post(`/api/projects/${pid}/watches`, {
    data: { name: 'Read-only watch', query: { kind: 'fts', q: 'budget', mode: 'keyword', rerank: 'off' } },
  });
  expect(created.ok()).toBeTruthy();
  const projects = await (await page.request.get('/api/projects')).json() as Array<Record<string, unknown>>;
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(projects.map((project) => project.id === pid ? { ...project, role: 'viewer' } : project)),
    });
  });

  await openProject(page, pid, sheetId);
  await openDiscoverTab(page, 'Watches');
  const item = page.getByTestId('watch-item').filter({ hasText: 'Read-only watch' });
  await expect(item).toBeVisible();
  await expect(item.getByTestId('run-watch')).toHaveCount(0);
  await expect(item.locator('[data-testid^="watch-refresh-index-"]')).toHaveCount(0);
  await expect(item.getByTestId('rename-watch')).toHaveCount(0);
  await expect(item.getByTestId('pause-watch')).toHaveCount(0);
  await expect(item.getByTestId('resume-watch')).toHaveCount(0);
  await expect(item.getByTestId('delete-watch')).toHaveCount(0);
  await expect(page.getByTestId('watch-create-composer')).toHaveCount(0);
});
