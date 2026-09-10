// RED-FIRST: Saved Views is a first-party Discover contribution. These are
// user-facing contracts for the list/create/edit flow, not the retired inline
// toolbar form.

import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openDiscoverTab,
  openFriendlyFilterSidebar,
  openProject,
  openToolbarOverflow,
  sheetColumns,
  uniqueName,
} from './helpers';

const filterSpec = { status: { eq: 'open' } };

function savedViewData(
  name: string,
  sheetId: number,
  definition: Partial<Record<'filter' | 'sort' | 'columns' | 'column_groups', unknown>> = {},
) {
  return {
    name,
    sheet_id: sheetId,
    filter: {},
    sort: null,
    columns: null,
    column_groups: null,
    ...definition,
  };
}

async function openViewsPanel(page: Page): Promise<void> {
  const panel = page.getByTestId('discover-panel');
  const rail = page.getByTestId('discover-rail');
  await expect(panel.or(rail).first()).toBeVisible();

  if (await rail.isVisible()) {
    await page.getByTestId('discover-rail-icon-Views').click();
  } else {
    const tab = page.getByTestId('discover-tab-Views');
    if (await tab.isVisible()) {
      await tab.click();
    } else {
      await page.getByTestId('discover-tab-overflow').click();
      await page.getByTestId('discover-tab-menu-Views').click();
    }
  }

  await expect(page.getByTestId('discover-tab-Views')).toHaveAttribute('aria-selected', 'true');
}

test('Saved Views is a sheet-scoped Discover contribution with list, create, and edit lifecycle', async ({
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
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // The panel is a proper first-party leftSidebar contribution, rendered by
  // Discover's right-hand frame rather than as an inline grid-control form.
  await openViewsPanel(page);
  const viewsContribution = page
    .getByTestId('discover-panel')
    .getByTestId('workbench-contribution-frisket-core-panel-saved-views');
  await expect(viewsContribution).toBeVisible();
  await expect(viewsContribution).toHaveAttribute('data-host', 'leftSidebar');
  await expect(viewsContribution).toHaveAttribute('data-slot', 'scope');
  await expect(viewsContribution.getByTestId('saved-views-list')).toBeVisible();

  // Save the currently applied filter. The sheet-bar action opens the create
  // state in Views; it does not mount a second, inline implementation.
  await openFriendlyFilterSidebar(page, columns, 'status');
  const filtered = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      url.searchParams.get('filter') === JSON.stringify(filterSpec)
    );
  });
  await page.getByTestId('facet-check-status-open').check();
  await filtered;

  await page.getByTestId('save-as-view').click();
  await expect(page.getByTestId('discover-tab-Views')).toHaveAttribute('aria-selected', 'true');
  const createForm = viewsContribution.getByTestId('saved-view-create');
  await expect(createForm).toBeVisible();
  await createForm.getByLabel('View name').fill('Open only');

  const createResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views`) &&
    res.request().method() === 'POST' &&
    res.status() === 200,
  );
  await createForm.getByRole('button', { name: 'Save view' }).click();
  const saved = await (await createResponse).json();
  expect(saved).toMatchObject({ name: 'Open only', sheet_id: sheetId, spec: { filter: filterSpec } });

  // Success returns to the list, where the persisted filter is legible.
  await expect(createForm).toHaveCount(0);
  const savedItem = viewsContribution.getByTestId('saved-view-item').filter({ hasText: 'Open only' });
  await expect(savedItem).toBeVisible();
  await expect(savedItem).toBeFocused();
  await expect(savedItem.getByTestId('saved-view-filter-summary')).toContainText(/status.*open/i);

  // Applying marks the grid as actively represented by this Saved View.
  await savedItem.getByTestId('apply-saved-view').click();
  await expect(savedItem.getByText('Active')).toBeVisible();
  await expect(page.getByTestId('active-saved-view-chip')).toContainText('Open only');
  await expect(page.getByText('Applied Open only', { exact: true })).toBeAttached();

  // A rename is metadata-only: changing the live grid must not overwrite the
  // stored filter spec, and PATCH carries precisely the new name.
  await page.getByTestId('clear-grid-filter').click();
  await expect(page.getByTestId('active-grid-filter')).toHaveCount(0);
  await savedItem.getByRole('button', { name: 'Rename Open only' }).click();
  const editForm = viewsContribution.getByTestId('saved-view-edit');
  await expect(editForm.getByTestId('saved-view-filter-summary')).toContainText(/status.*open/i);
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
  expect(await patch.json()).toMatchObject({ name: 'Open status', spec: { filter: filterSpec } });

  await expect(editForm).toHaveCount(0);
  const renamedItem = viewsContribution.getByTestId('saved-view-item').filter({ hasText: 'Open status' });
  await expect(renamedItem).toBeVisible();
  await expect(renamedItem).toBeFocused();
  const stored = await (await page.request.get(`/api/projects/${pid}/views?sheet_id=${sheetId}`)).json();
  expect(stored.find((view: { id: number }) => view.id === saved.id)).toMatchObject({
    name: 'Open status',
    spec: { filter: filterSpec },
  });

  // A definition update is deliberately separate from Rename. It captures
  // the current grid only after confirmation; Escape makes no request.
  const updateAction = renamedItem.getByTestId('update-saved-view-definition');
  await updateAction.click();
  const updateDialog = page.getByTestId('update-saved-view-confirmation');
  await expect(updateDialog).toBeVisible();
  await expect(updateDialog).toContainText('Replace this Saved View’s filters');
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
  await page.getByTestId('confirm-update-saved-view').click();
  const definition = await definitionResponse;
  expect(definition.request().postDataJSON()).toMatchObject({
    filter: {},
    sort: null,
    columns: null,
    column_groups: null,
  });
  await expect(renamedItem).toBeFocused();
  await expect(page.getByText('Saved current display to Open status.', { exact: true })).toBeAttached();

  const deleteResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views/${saved.id}`) &&
    res.request().method() === 'DELETE' &&
    res.status() === 200,
  );
  await viewsContribution
    .getByTestId('saved-view-item')
    .filter({ hasText: 'Open status' })
    .getByTestId('delete-saved-view')
    .click();
  const deleteDialog = page.getByTestId('delete-saved-view-confirmation');
  await expect(deleteDialog).toBeVisible();
  await expect(deleteDialog).toContainText('Deleting this Saved View cannot be undone.');
  await deleteDialog.getByTestId('confirm-delete-saved-view').click();
  await deleteResponse;
  await expect(
    viewsContribution.getByTestId('saved-view-item').filter({ hasText: 'Open status' }),
  ).toHaveCount(0);
  await expect(viewsContribution.getByTestId('saved-views-list')).toBeFocused();
  expect((await page.request.get(`/api/projects/${pid}/views/${saved.id}`)).status()).toBe(404);
});

test('Saved Views overflow offers new, apply, and open-panel shortcuts', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-overflow'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'status\nopen\nclosed\n');
  const seeded = await (
    await page.request.post(`/api/projects/${pid}/views`, {
      data: savedViewData('Open only', sheetId, { filter: filterSpec }),
    })
  ).json();
  await page.request.post(`/api/projects/${pid}/views`, {
    data: savedViewData('All rows', sheetId),
  });
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await openToolbarOverflow(page);
  const savedViewsTrigger = page.getByTestId('open-saved-views-submenu');
  await savedViewsTrigger.hover();
  const menu = page.getByTestId('saved-views-overflow-menu');
  await expect(menu).toBeVisible();
  const overflowBox = await page.getByTestId('toolbar-overflow-menu').boundingBox();
  const menuBox = await menu.boundingBox();
  expect(overflowBox).not.toBeNull();
  expect(menuBox).not.toBeNull();
  expect(
    menuBox!.x + menuBox!.width <= overflowBox!.x - 3 ||
      menuBox!.x >= overflowBox!.x + overflowBox!.width + 3,
  ).toBeTruthy();
  expect(menuBox!.x).toBeGreaterThanOrEqual(8);
  expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(1592);
  expect(menuBox!.y).toBeGreaterThanOrEqual(8);
  expect(menuBox!.y + menuBox!.height).toBeLessThanOrEqual(892);

  await menu.getByTestId('saved-views-overflow-new').click();
  await expect(page.getByTestId('saved-view-create')).toBeVisible();
  await page.getByTestId('saved-view-editor-cancel').click();

  await openToolbarOverflow(page);
  await savedViewsTrigger.focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByTestId(`saved-views-overflow-view-${seeded.id}`)).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(savedViewsTrigger).toBeFocused();
  await expect(menu).toBeHidden();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByTestId(`saved-views-overflow-view-${seeded.id}`)).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(savedViewsTrigger).toBeFocused();
  await expect(menu).toBeHidden();
  await page.keyboard.press('ArrowRight');
  const applyResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      url.searchParams.get('filter') === JSON.stringify(filterSpec)
    );
  });
  await page.getByTestId(`saved-views-overflow-view-${seeded.id}`).click();
  await applyResponse;
  await expect(page.getByTestId('active-grid-filter')).toContainText(/status.*open/i);
  await expect(page.getByText('Applied Open only', { exact: true })).toBeAttached();

  // The menu closes after application, so reopen it to verify the same
  // active marker appears at this second apply entry point too.
  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await expect(page.getByTestId(`saved-views-overflow-view-${seeded.id}`)).toContainText('Active');

  await page.getByTestId('saved-views-overflow-open-panel').click();
  await expect(page.getByTestId('discover-tab-Views')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('saved-views-list')).toBeVisible();
  await expect(
    page
      .getByTestId('saved-view-item')
      .filter({ hasText: 'All rows' })
      .getByTestId('saved-view-filter-summary'),
  ).toHaveText('No filters');
});

test('a delayed Sheet A response cannot replace the active Sheet B Views list', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-stale-list'));
  const sheetA = await importCsv(page.request, pid, 'a.csv', 'status\nopen\n');
  const sheetB = await importCsv(page.request, pid, 'b.csv', 'status\nclosed\n');
  await page.request.post(`/api/projects/${pid}/views`, {
    data: savedViewData('Sheet A view', sheetA, { filter: filterSpec }),
  });
  await page.request.post(`/api/projects/${pid}/views`, {
    data: savedViewData('Sheet B view', sheetB),
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
        parsed.pathname === `/api/projects/${pid}/views` &&
        parsed.searchParams.get('sheet_id') === String(sheetA)
      );
    },
    async (route) => {
      observeSheetARequest();
      await delayedSheetA;
      await route.continue();
    },
  );

  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  await sheetARequested;
  await page.getByTestId(`workbench-mainView-tab-${sheetB}`).click();
  const views = page.getByTestId('saved-views-list');
  await expect(views.getByTestId('saved-view-item').filter({ hasText: 'Sheet B view' })).toBeVisible();

  releaseSheetA();
  await expect(views.getByTestId('saved-view-item').filter({ hasText: 'Sheet B view' })).toBeVisible();
  await expect(views.getByTestId('saved-view-item').filter({ hasText: 'Sheet A view' })).toHaveCount(0);
});

test('same-sheet Saved View publications retire older list success and failure', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-publication-race'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'status\nopen\n');
  const existing = await (
    await page.request.post(`/api/projects/${pid}/views`, {
      data: savedViewData('Existing view', sheetId),
    })
  ).json();
  await openProject(page, pid, sheetId);
  await openViewsPanel(page);
  await expect(page.getByTestId('saved-view-item').filter({ hasText: 'Existing view' })).toBeVisible();

  await openDiscoverTab(page, 'Sources');
  let releaseStaleCreateList!: () => void;
  const staleCreateList = new Promise<void>((resolve) => { releaseStaleCreateList = resolve; });
  let observeStaleCreateList!: () => void;
  const staleCreateListRequested = new Promise<void>((resolve) => { observeStaleCreateList = resolve; });
  await page.route(`**/api/projects/${pid}/views?sheet_id=${sheetId}`, async (route) => {
    observeStaleCreateList();
    await staleCreateList;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify([existing]) });
  }, { times: 1 });
  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  await staleCreateListRequested;

  await page.getByTestId('create-saved-view').click();
  const createForm = page.getByTestId('saved-view-create');
  await createForm.getByLabel('View name').fill('Published view');
  const createResponse = page.waitForResponse((response) =>
    response.request().method() === 'POST' &&
    new URL(response.url()).pathname === `/api/projects/${pid}/views`,
  );
  await createForm.getByRole('button', { name: 'Save view' }).click();
  const created = await (await createResponse).json();
  const published = page.getByTestId('saved-view-item').filter({ hasText: 'Published view' });
  await expect(published).toBeFocused();
  releaseStaleCreateList();
  await expect(published).toBeVisible();

  await openDiscoverTab(page, 'Sources');
  let releaseStaleDeleteList!: () => void;
  const staleDeleteList = new Promise<void>((resolve) => { releaseStaleDeleteList = resolve; });
  let observeStaleDeleteList!: () => void;
  const staleDeleteListRequested = new Promise<void>((resolve) => { observeStaleDeleteList = resolve; });
  await page.route(`**/api/projects/${pid}/views?sheet_id=${sheetId}`, async (route) => {
    observeStaleDeleteList();
    await staleDeleteList;
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify([existing, created]) });
  }, { times: 1 });
  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  await staleDeleteListRequested;
  await published.getByTestId('delete-saved-view').click();
  await page.getByTestId('confirm-delete-saved-view').click();
  await expect(published).toHaveCount(0);
  releaseStaleDeleteList();
  await expect(page.getByTestId('saved-view-item').filter({ hasText: 'Published view' })).toHaveCount(0);
  await expect(page.getByTestId('saved-view-item').filter({ hasText: 'Existing view' })).toBeFocused();

  await openDiscoverTab(page, 'Sources');
  let releaseStaleFailure!: () => void;
  const staleFailure = new Promise<void>((resolve) => { releaseStaleFailure = resolve; });
  let requestNumber = 0;
  await page.route(`**/api/projects/${pid}/views?sheet_id=${sheetId}`, async (route) => {
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
  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  // Opening the populated submenu loads its entries, then Open panel performs
  // the semantic reveal + load. The first response remains delayed while the
  // newer request publishes successfully.
  await expect.poll(() => requestNumber).toBe(2);
  await expect(page.getByTestId('saved-view-item').filter({ hasText: 'Existing view' })).toBeVisible();
  releaseStaleFailure();
  await expect(page.getByText('stale Saved View failure')).toHaveCount(0);
});

test('viewers can apply Saved Views but have no creation or mutation entry point', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-viewer'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'status\nopen\n');
  const saved = await (await page.request.post(`/api/projects/${pid}/views`, {
    data: savedViewData('Viewer can apply', sheetId),
  })).json();
  const projects = await (await page.request.get('/api/projects')).json() as Array<Record<string, unknown>>;
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify(projects.map((project) => project.id === pid ? { ...project, role: 'viewer' } : project)),
    });
  });

  await openProject(page, pid, sheetId);
  await openViewsPanel(page);
  const row = page.getByTestId('saved-view-item').filter({ hasText: 'Viewer can apply' });
  await expect(row.getByTestId('apply-saved-view')).toBeVisible();
  await expect(page.getByTestId('create-saved-view')).toHaveCount(0);
  await expect(page.getByTestId('save-as-view')).toHaveCount(0);
  await expect(row.getByTestId('edit-saved-view')).toHaveCount(0);
  await expect(row.getByTestId('update-saved-view-definition')).toHaveCount(0);
  await expect(row.getByTestId('delete-saved-view')).toHaveCount(0);

  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await expect(page.getByTestId('saved-views-overflow-new')).toHaveCount(0);
  await expect(page.getByTestId(`saved-views-overflow-view-${saved.id}`)).toBeVisible();
});

test('Saved Views editor actions remain usable at Discover’s constrained width', async ({ page }) => {
  await page.setViewportSize({ width: 640, height: 760 });
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-narrow'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'status\nopen\n');
  await page.addInitScript(
    ([projectId]) => localStorage.setItem(`frisket:discover-width:${projectId}`, '220'),
    [pid],
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openViewsPanel(page);
  const discover = page.getByTestId('discover-panel');
  const discoverBox = await discover.boundingBox();
  expect(discoverBox?.width).toBeGreaterThanOrEqual(219);
  expect(discoverBox?.width).toBeLessThanOrEqual(221);

  await page.getByTestId('create-saved-view').click();
  const createForm = page.getByTestId('saved-view-create');
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

  const item = page.getByTestId('saved-view-item').filter({ hasText: 'Narrow view' });
  const rename = item.getByRole('button', { name: 'Rename Narrow view' });
  const updateDefinition = item.getByRole('button', { name: 'Save current display to Narrow view' });
  await expect(rename).toBeVisible();
  await expect(updateDefinition).toBeVisible();
  await rename.click();
  const editForm = page.getByTestId('saved-view-edit');
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
  await expect(page.getByTestId('saved-view-item').filter({ hasText: 'Narrow view renamed' })).toBeVisible();
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
  await page.request.post(`/api/projects/${pid}/views`, {
    data: savedViewData('Open cities', sheetId, {
      filter: filterSpec,
      sort: [{ column: 'city', dir: 'asc' }],
      columns: ['city', 'status'],
    }),
  });
  await page.request.post(`/api/projects/${pid}/views`, {
    data: savedViewData('Everything', sheetId),
  });
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openViewsPanel(page);

  const views = page.getByTestId('saved-views-list');
  await expect(views.getByTestId('saved-view-item').filter({ hasText: 'Open cities' }))
    .toContainText('status equals open · city ascending · 2 columns');
  await expect(views.getByTestId('saved-view-item').filter({ hasText: 'Everything' }))
    .toContainText('No filters');
  await expect(views.getByTestId('saved-view-item').filter({ hasText: 'Everything' }))
    .not.toContainText(/columns|ascending/i);
});

test('Saved Views overflow adds local search only above eight views and resets it when closed', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-overflow-search'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'status\nopen\n');
  const views = await Promise.all(
    Array.from({ length: 9 }, (_, index) =>
      page.request
        .post(`/api/projects/${pid}/views`, {
          data: savedViewData(`Saved view ${index + 1}`, sheetId),
        })
        .then((response) => response.json()),
    ),
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  const menu = page.getByTestId('saved-views-overflow-menu');
  const list = menu.getByTestId('saved-views-overflow-list');
  await expect(menu.getByTestId('saved-views-overflow-count')).toHaveCount(0);
  await expect(menu.getByLabel('Search saved views')).toBeVisible();
  await expect(list).toHaveCount(1);
  await expect.poll(() => list.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);

  let listRequests = 0;
  page.on('request', (request) => {
    if (
      request.method() === 'GET' &&
      new URL(request.url()).pathname === `/api/projects/${pid}/views`
    ) {
      listRequests += 1;
    }
  });
  const search = menu.getByLabel('Search saved views');
  await search.fill('VIEW 9');
  await expect(list.getByTestId(`saved-views-overflow-view-${views[8].id}`)).toBeVisible();
  await expect(list.getByTestId(`saved-views-overflow-view-${views[0].id}`)).toHaveCount(0);
  expect(listRequests).toBe(0);

  await search.fill('does not exist');
  await expect(menu.getByText('No matching saved views')).toBeVisible();
  await expect(menu.getByTestId('saved-views-overflow-new')).toBeVisible();
  await expect(menu.getByTestId('saved-views-overflow-open-panel')).toBeVisible();

  await page.keyboard.press('Escape');
  await expect(menu).not.toBeVisible();
  await expect(page.getByTestId('toolbar-overflow-menu')).toBeVisible();
  await page.getByTestId('open-saved-views-submenu').click();
  await expect(page.getByTestId('saved-views-overflow-menu').getByLabel('Search saved views')).toHaveValue('');
});

test('Saved Views overflow does not show a search field at exactly eight views', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-saved-views-overflow-eight'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'status\nopen\n');
  await Promise.all(
    Array.from({ length: 8 }, (_, index) =>
      page.request.post(`/api/projects/${pid}/views`, {
        data: savedViewData(`Saved view ${index + 1}`, sheetId),
      }),
    ),
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();

  const menu = page.getByTestId('saved-views-overflow-menu');
  await expect(menu.getByTestId('saved-views-overflow-count')).toHaveCount(0);
  await expect(menu.getByLabel('Search saved views')).toHaveCount(0);
});
