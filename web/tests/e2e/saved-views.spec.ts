// Views are the second tab of the sheet's Filter sidebar (the same panel also
// renders in Discover). These are user-facing contracts for the list, create,
// apply, rename, update and delete flow.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openDiscoverTab,
  openFriendlyFilterSidebar,
  openProject,
  sheetColumns,
  uniqueName,
  openViewsTab,
} from './helpers';

function viewData(
  name: string,
  sheetId: number,
  definition: { filters?: { column_id: number; condition: Record<string, string> }[]; order?: { column_id: number; dir: 'asc' | 'desc' }[]; columns?: number[] } = {},
) {
  return {
    name,
    selection: { scope: { kind: 'sheet', sheet_id: sheetId }, filters: definition.filters ?? [], order: definition.order ?? [], limit: null },
    presentation: { columns: definition.columns ?? null, column_groups: null },
  };
}

function hasOpenFilter(url: URL, columnId: number): boolean {
  const selection = JSON.parse(url.searchParams.get('selection') ?? '{}');
  return selection.filters?.some((item: { column_id: number; condition: { eq?: string } }) => item.column_id === columnId && item.condition.eq === 'open') ?? false;
}

test('Views is a sheet-scoped sidebar tab with list, create, and edit lifecycle', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status\nAlbany,open\nBuffalo,closed\nSyracuse,open\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const statusColumnId = columns.find((column) => column.name === 'status')!.id;
  const filters = [{ column_id: statusColumnId, condition: { eq: 'open' } }];
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // The panel is the first-party Views contribution, rendered in the sheet's
  // Filter sidebar.
  await openViewsTab(page);
  const viewsContribution = page
    .getByTestId('filter-sidebar')
    .getByTestId('workbench-contribution-frisket-core-panel-views');
  await expect(viewsContribution).toBeVisible();
  await expect(viewsContribution.getByTestId('views-list')).toBeVisible();
  await expect(viewsContribution.getByTestId('view-item-all-rows')).toHaveAttribute('data-current', 'true');

  // Save the currently applied filter. The chip strip's Save as view opens
  // the create form on the Views tab.
  await openFriendlyFilterSidebar(page, columns, 'status');
  const filtered = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      hasOpenFilter(url, statusColumnId)
    );
  });
  await page.getByTestId('facet-check-status-open').check();
  await filtered;

  await page.getByTestId('save-as-view').click();
  await expect(page.getByTestId('filter-sidebar-tab-views')).toHaveAttribute('aria-selected', 'true');
  const createForm = viewsContribution.getByTestId('view-create');
  await expect(createForm).toBeVisible();
  await expect(createForm.getByLabel('View name')).toBeFocused();
  await expect(createForm.getByTestId('view-create-summary')).toHaveText('Saving: status open · 2 columns');
  await createForm.getByLabel('View name').fill('Open only');

  const createResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views`) &&
    res.request().method() === 'POST' &&
    res.status() === 200,
  );
  await createForm.getByRole('button', { name: 'Save view' }).click();
  const saved = await (await createResponse).json();
  expect(saved).toMatchObject({ name: 'Open only', sheet_id: sheetId, spec: { selection: { filters } } });

  // Success returns to the list, where the persisted filter is legible.
  await expect(createForm).toHaveCount(0);
  const savedItem = viewsContribution.getByTestId('view-item').filter({ hasText: 'Open only' });
  await expect(savedItem).toBeVisible();
  await expect(savedItem).toBeFocused();
  await expect(savedItem.getByTestId('view-filter-summary')).toContainText('1 filter');

  // The saved view matches the current state, so it is the current view.
  await expect(savedItem).toHaveAttribute('data-current', 'true');
  await savedItem.getByTestId('apply-view').click();
  await expect(savedItem).toHaveAttribute('data-current', 'true');
  await expect(page.getByText('Applied Open only', { exact: true })).toBeAttached();
  await page.screenshot({ path: 'test-results/domain-saved-view.png', fullPage: true });

  // A rename is metadata-only: changing the live grid must not overwrite the
  // stored filter spec, and PATCH carries precisely the new name.
  await page.getByTestId('row-filter-chip-remove-facet:status').click();
  await expect(page.getByTestId('row-filter-chips')).toHaveCount(0);
  await expect(savedItem).not.toHaveAttribute('data-current', 'true');
  await savedItem.getByRole('button', { name: 'Rename Open only' }).click();
  const editForm = viewsContribution.getByTestId('view-edit');
  await expect(editForm.getByTestId('view-filter-summary')).toContainText('1 filter');
  const nameInput = editForm.getByLabel('View name');
  const renameButton = editForm.getByRole('button', { name: 'Rename view' });
  await nameInput.fill('   ');
  await expect(renameButton).toBeDisabled();
  await expect(nameInput).toHaveAccessibleDescription('Enter a name to continue.');

  let invalidRenameRequests = 0;
  page.on('request', (request) => {
    if (
      request.method() === 'PATCH' &&
      request.url().includes(`/api/projects/${pid}/views/${saved.id}`)
    ) {
      invalidRenameRequests += 1;
    }
  });
  await renameButton.click({ force: true });
  expect(invalidRenameRequests).toBe(0);

  await nameInput.fill('Open status');
  await expect(renameButton).toBeEnabled();
  const patchResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views/${saved.id}`) &&
    res.request().method() === 'PATCH' &&
    res.status() === 200,
  );
  await renameButton.click();
  const patch = await patchResponse;
  expect(patch.request().postDataJSON()).toEqual({ name: 'Open status' });
  expect(await patch.json()).toMatchObject({ name: 'Open status', spec: { selection: { filters } } });

  await expect(editForm).toHaveCount(0);
  const renamedItem = viewsContribution.getByTestId('view-item').filter({ hasText: 'Open status' });
  await expect(renamedItem).toBeVisible();
  await expect(renamedItem).toBeFocused();
  const stored = await (await page.request.get(`/api/projects/${pid}/views?sheet_id=${sheetId}`)).json();
  expect(stored.find((view: { id: number }) => view.id === saved.id)).toMatchObject({
    name: 'Open status',
    spec: { selection: { filters } },
  });

  // A definition update is deliberately separate from Rename. It captures
  // the current grid only after confirmation; Escape makes no request.
  const updateAction = renamedItem.getByTestId('update-view-definition');
  await updateAction.click();
  const updateDialog = page.getByTestId('update-view-confirmation');
  await expect(updateDialog).toBeVisible();
  await expect(updateDialog).toContainText('current selection');
  let definitionRequests = 0;
  page.on('request', (request) => {
    if (
      request.method() === 'PUT' &&
      request.url().includes(`/api/projects/${pid}/views/${saved.id}/definition`)
    ) {
      definitionRequests += 1;
    }
  });
  await page.keyboard.press('Escape');
  await expect(updateDialog).toHaveCount(0);
  expect(definitionRequests).toBe(0);
  await expect(updateAction).toBeFocused();

  await updateAction.click();
  const definitionResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views/${saved.id}/definition`) &&
    res.request().method() === 'PUT' &&
    res.status() === 200,
  );
  await page.getByTestId('confirm-update-view').click();
  const definition = await definitionResponse;
  expect(definition.request().postDataJSON()).toMatchObject({
    selection: { scope: { kind: 'sheet', sheet_id: sheetId }, filters: [], order: [] },
    presentation: { columns: null, column_groups: null },
  });
  await expect(renamedItem).toBeFocused();
  await expect(page.getByText('Saved changes to Open status.', { exact: true })).toBeAttached();

  const deleteResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views/${saved.id}`) &&
    res.request().method() === 'DELETE' &&
    res.status() === 200,
  );
  await viewsContribution
    .getByTestId('view-item')
    .filter({ hasText: 'Open status' })
    .getByTestId('delete-view')
    .click();
  const deleteDialog = page.getByTestId('delete-view-confirmation');
  await expect(deleteDialog).toBeVisible();
  await expect(deleteDialog).toContainText('Deleting this view cannot be undone.');
  await deleteDialog.getByTestId('confirm-delete-view').click();
  await deleteResponse;
  await expect(
    viewsContribution.getByTestId('view-item').filter({ hasText: 'Open status' }),
  ).toHaveCount(0);
  await expect(viewsContribution.getByTestId('views-list')).toBeFocused();
  expect((await page.request.get(`/api/projects/${pid}/views/${saved.id}`)).status()).toBe(404);
});

test('the Views tab applies a view, marks the current view and saves from the keyboard', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-tab'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'status\nopen\nclosed\n');
  const statusColumnId = (await sheetColumns(page.request, pid, sheetId)).find((column) => column.name === 'status')!.id;
  const seeded = await (
    await page.request.post(`/api/projects/${pid}/views`, {
      data: viewData('Open only', sheetId, { filters: [{ column_id: statusColumnId, condition: { eq: 'open' } }] }),
    })
  ).json();
  await page.request.post(`/api/projects/${pid}/views`, {
    data: viewData('Everything', sheetId),
  });
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await openViewsTab(page);
  const panel = page.getByTestId('filter-sidebar').getByTestId('views-panel');
  // Arrow keys move between the sidebar's tabs.
  await page.getByTestId('filter-sidebar-tab-views').focus();
  await page.keyboard.press('ArrowLeft');
  await expect(page.getByTestId('filter-sidebar-tab-filter')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('filter-sidebar-tab-filter')).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByTestId('filter-sidebar-tab-views')).toHaveAttribute('aria-selected', 'true');

  // Nothing is active: All rows and the unfiltered saved view both match.
  await expect(panel.getByTestId('view-item-all-rows')).toHaveAttribute('data-current', 'true');
  const openOnly = panel.getByTestId('view-item').filter({ hasText: 'Open only' });
  await expect(openOnly).not.toHaveAttribute('data-current', 'true');

  const applyResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      hasOpenFilter(url, statusColumnId)
    );
  });
  await openOnly.getByTestId('apply-view').click();
  await applyResponse;
  await expect(page.getByTestId('row-filter-chip-facet:status')).toContainText('status open');
  await expect(page.getByTestId('sheet-stats')).toHaveText('1 of 2 rows · 1 column');
  await expect(openOnly).toHaveAttribute('data-current', 'true');
  await expect(panel.getByTestId('view-item-all-rows')).not.toHaveAttribute('data-current', 'true');
  await expect(page.getByText('Applied Open only', { exact: true })).toBeAttached();
  await page.screenshot({ path: 'test-results/domain-saved-view.png', fullPage: true });

  // All rows resets the sheet.
  await panel.getByTestId('apply-all-rows').click();
  await expect(page.getByTestId('row-filter-chips')).toHaveCount(0);
  await expect(panel.getByTestId('view-item-all-rows')).toHaveAttribute('data-current', 'true');

  // Save current view opens the form; Escape cancels it.
  await panel.getByTestId('create-view').click();
  await expect(panel.getByTestId('view-name-input')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(panel.getByTestId('view-create')).toHaveCount(0);
  await expect(page.getByTestId('filter-sidebar')).toBeVisible();
  expect(seeded.id).toBeGreaterThan(0);
});

test('a delayed Sheet A response cannot replace the active Sheet B Views list', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-stale-list'));
  const sheetA = await importCsv(page.request, pid, 'a.csv', 'status\nopen\n');
  const sheetB = await importCsv(page.request, pid, 'b.csv', 'status\nclosed\n');
  await page.request.post(`/api/projects/${pid}/views`, {
    data: viewData('Sheet A view', sheetA, { filters: [{ column_id: (await sheetColumns(page.request, pid, sheetA))[0].id, condition: { eq: 'open' } }] }),
  });
  await page.request.post(`/api/projects/${pid}/views`, {
    data: viewData('Sheet B view', sheetB),
  });
  await openProject(page, pid, sheetA);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  let releaseSheetA!: () => void;
  const delayedSheetA = new Promise<void>((resolve) => {
    releaseSheetA = resolve;
  });
  let observeSheetARequest!: () => void;
  const sheetARequested = new Promise<void>((resolve) => {
    observeSheetARequest = resolve;
  });
  await page.route(
    (url) => {
      const parsed = new URL(url);
      return (
        parsed.pathname === `/api/projects/${pid}/views`
      );
    },
    async (route) => {
      observeSheetARequest();
      await delayedSheetA;
      await route.continue();
    },
    { times: 1 },
  );

  await openViewsTab(page);
  await sheetARequested;
  await page.getByTestId(`workbench-mainView-tab-${sheetB}`).click();
  const views = page.getByTestId('views-list');
  await expect(views.getByTestId('view-item').filter({ hasText: 'Sheet B view' })).toBeVisible();

  releaseSheetA();
  await expect(views.getByTestId('view-item').filter({ hasText: 'Sheet B view' })).toBeVisible();
  await expect(views.getByTestId('view-item').filter({ hasText: 'Sheet A view' })).toHaveCount(0);
});

test('same-sheet Saved View publications retire older list success and failure', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-publication-race'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'status\nopen\n');
  const existing = await (
    await page.request.post(`/api/projects/${pid}/views`, {
      data: viewData('Existing view', sheetId),
    })
  ).json();
  await openProject(page, pid, sheetId);
  await openViewsTab(page);
  await expect(page.getByTestId('view-item').filter({ hasText: 'Existing view' })).toBeVisible();

  await openDiscoverTab(page, 'Sources');
  let releaseStaleCreateList!: () => void;
  const staleCreateList = new Promise<void>((resolve) => { releaseStaleCreateList = resolve; });
  let observeStaleCreateList!: () => void;
  const staleCreateListRequested = new Promise<void>((resolve) => { observeStaleCreateList = resolve; });
  await page.route(`**/api/projects/${pid}/views`, async (route) => {
    observeStaleCreateList();
    await staleCreateList;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify([existing]) });
  }, { times: 1 });
  await openViewsTab(page);
  await staleCreateListRequested;

  await page.getByTestId('create-view').click();
  const createForm = page.getByTestId('view-create');
  await createForm.getByLabel('View name').fill('Published view');
  const createResponse = page.waitForResponse((response) =>
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === `/api/projects/${pid}/views`,
  );
  await createForm.getByRole('button', { name: 'Save view' }).click();
  const created = await (await createResponse).json();
  const published = page.getByTestId('view-item').filter({ hasText: 'Published view' });
  await expect(published).toBeFocused();
  releaseStaleCreateList();
  await expect(published).toBeVisible();

  await openDiscoverTab(page, 'Sources');
  let releaseStaleDeleteList!: () => void;
  const staleDeleteList = new Promise<void>((resolve) => { releaseStaleDeleteList = resolve; });
  let observeStaleDeleteList!: () => void;
  const staleDeleteListRequested = new Promise<void>((resolve) => { observeStaleDeleteList = resolve; });
  await page.route(`**/api/projects/${pid}/views`, async (route) => {
    observeStaleDeleteList();
    await staleDeleteList;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify([existing, created]) });
  }, { times: 1 });
  await openViewsTab(page);
  await staleDeleteListRequested;
  await published.getByTestId('delete-view').click();
  await page.getByTestId('confirm-delete-view').click();
  await expect(published).toHaveCount(0);
  releaseStaleDeleteList();
  await expect(page.getByTestId('view-item').filter({ hasText: 'Published view' })).toHaveCount(0);
  await expect(page.getByTestId('view-item').filter({ hasText: 'Existing view' })).toBeFocused();

  await openDiscoverTab(page, 'Sources');
  let releaseStaleFailure!: () => void;
  const staleFailure = new Promise<void>((resolve) => { releaseStaleFailure = resolve; });
  let requestNumber = 0;
  await page.route(`**/api/projects/${pid}/views`, async (route) => {
    requestNumber += 1;
    if (requestNumber === 1) {
      await staleFailure;
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'stale Saved View failure' }),
      });
      return;
    }
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify([existing]) });
  });
  await openViewsTab(page);
  // Opening the populated submenu loads its entries, then Open panel performs
  // the semantic reveal + load. The first response remains delayed while the
  // newer request publishes successfully.
  await expect.poll(() => requestNumber).toBe(2);
  await expect(page.getByTestId('view-item').filter({ hasText: 'Existing view' })).toBeVisible();
  releaseStaleFailure();
  await expect(page.getByText('stale Saved View failure')).toHaveCount(0);
});

test('viewers can apply Saved Views but have no creation or mutation entry point', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-viewer'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'status\nopen\n');
  const saved = await (await page.request.post(`/api/projects/${pid}/views`, {
    data: viewData('Viewer can apply', sheetId),
  })).json();
  const projects = await (await page.request.get('/api/projects')).json() as Array<Record<string, unknown>>;
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(projects.map((project) => project.id === pid ? { ...project, role: 'viewer' } : project)),
    });
  });

  await openProject(page, pid, sheetId);
  await openViewsTab(page);
  const row = page.getByTestId('view-item').filter({ hasText: 'Viewer can apply' });
  await expect(row.getByTestId('apply-view')).toBeVisible();
  await expect(page.getByTestId('create-view')).toHaveCount(0);
  await expect(page.getByTestId('save-as-view')).toHaveCount(0);
  await expect(row.getByTestId('edit-view')).toHaveCount(0);
  await expect(row.getByTestId('update-view-definition')).toHaveCount(0);
  await expect(row.getByTestId('delete-view')).toHaveCount(0);
  expect(saved.id).toBeGreaterThan(0);
});

test('Views editor actions remain usable in the sidebar on a narrow window', async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 760 });
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-narrow'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'status\nopen\n');
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openViewsTab(page);
  const sidebarBox = await page.getByTestId('filter-sidebar').boundingBox();
  expect(sidebarBox?.width).toBeGreaterThanOrEqual(299);
  expect(sidebarBox?.width).toBeLessThanOrEqual(301);

  await page.getByTestId('create-view').click();
  const createForm = page.getByTestId('view-create');
  const createName = createForm.getByLabel('View name');
  const save = createForm.getByRole('button', { name: 'Save view' });
  const cancel = createForm.getByRole('button', { name: 'Cancel' });
  await expect(createName).toBeVisible();
  await expect(save).toBeVisible();
  await expect(cancel).toBeVisible();
  await createName.fill('Narrow view');

  const createResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views`) &&
    res.request().method() === 'POST' &&
    res.status() === 200,
  );
  await save.click();
  const saved = await (await createResponse).json();
  await expect(createForm).toHaveCount(0);

  const item = page.getByTestId('view-item').filter({ hasText: 'Narrow view' });
  const rename = item.getByRole('button', { name: 'Rename Narrow view' });
  const updateDefinition = item.getByRole('button', { name: 'Save changes to Narrow view' });
  await expect(rename).toBeVisible();
  await expect(updateDefinition).toBeVisible();
  await rename.click();
  const editForm = page.getByTestId('view-edit');
  const update = editForm.getByRole('button', { name: 'Rename view' });
  await expect(update).toBeVisible();
  await editForm.getByLabel('View name').fill('Narrow view renamed');

  const renameResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views/${saved.id}`) &&
    res.request().method() === 'PATCH' &&
    res.status() === 200,
  );
  await update.click();
  await renameResponse;
  const panel = page.getByTestId('views-panel');
  await expect(page.getByTestId('view-item').filter({ hasText: 'Narrow view renamed' })).toBeVisible();
  expect(await panel.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
});

test('Saved Views summarizes filters, sorting, and custom displayed columns in one compact label', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-summary'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status,owner\nAlbany,open,Ada\nBuffalo,closed,Grace\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await page.request.post(`/api/projects/${pid}/views`, {
    data: viewData('Open cities', sheetId, {
      filters: [{ column_id: (await sheetColumns(page.request, pid, sheetId)).find((column) => column.name === 'status')!.id, condition: { eq: 'open' } }],
      order: [{ column_id: (await sheetColumns(page.request, pid, sheetId)).find((column) => column.name === 'city')!.id, dir: 'asc' }],
      columns: [columns.find(column => column.name === 'city')!.id, columns.find(column => column.name === 'status')!.id],
    }),
  });
  await page.request.post(`/api/projects/${pid}/views`, {
    data: viewData('Everything', sheetId),
  });
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openViewsTab(page);

  const views = page.getByTestId('views-list');
  await expect(views.getByTestId('view-item').filter({ hasText: 'Open cities' }))
    .toContainText('1 filter · 1 sort · 2 columns');
  await expect(views.getByTestId('view-item').filter({ hasText: 'Everything' }))
    .toContainText('No filters');
  await expect(views.getByTestId('view-item').filter({ hasText: 'Everything' }))
    .not.toContainText(/columns|ascending/i);
});
