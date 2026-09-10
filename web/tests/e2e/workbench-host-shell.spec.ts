import { expect, test, type Page } from '@playwright/test';
import {
  listSheets,
  openCellDrawer,
  openDiscoverTab,
  openProject,
  projectIdByName,
  sheetColumns,
  type WireColumn,
} from './helpers';

async function openCommandPalette(page: Page) {
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  return palette;
}

let pid: string;
let columns: WireColumn[];

test.beforeEach(async ({ page }) => {
  pid = await projectIdByName(page.request, 'Local stories');
  const sheets = await listSheets(page.request, pid);
  columns = await sheetColumns(page.request, pid, sheets[0].id);
  await openProject(page, pid, sheets[0].id);
});

test('workbench host shell exposes fixed regions while the grid remains usable', async ({ page }) => {
  const shell = page.getByTestId('workbench-shell');
  await expect(shell).toBeVisible();

  // The left sidebar region and the activity rail are retired
  // (workbench-ia-focus-v1 / the redesign purge).
  await expect(page.getByTestId('workbench-region-leftSidebar')).toHaveCount(0);
  await expect(page.getByTestId('workbench-region-mainView')).toBeVisible();
  // The right inspector collapses to zero width when empty (it hosts only the
  // cluster-resolve + plugin panels since the right-edge redesign) — attached,
  // not necessarily visible.
  await expect(page.getByTestId('workbench-region-rightInspector')).toBeAttached();
  await expect(page.getByTestId('workbench-region-bottomDock')).toBeVisible();
  await expect(page.getByTestId('workbench-region-modalOrPeek')).toBeAttached();

  await expect(page.getByTestId('workbench-region-right')).toHaveCount(0);
  await expect(page.getByTestId('workbench-region-bottom')).toHaveCount(0);

  const mainView = page.getByTestId('workbench-region-mainView');
  await expect(mainView.getByTestId('workbench-mainView-host')).toBeVisible();
  await expect(mainView.getByTestId('workbench-contribution-frisket-core-view-grid')).toBeVisible();
  await expect(mainView.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('sheet-stats')).toHaveText('8 rows · 6 columns');

  // Availability surfaces (the activity rail retired; the ⌘K palette carries
  // the visibility state): no geo column in this project, so the map
  // view-switcher segment is data-omitted and the palette's map command
  // advertises the disabled state + real reason.
  await expect(page.getByTestId('view-switch-map')).toHaveCount(0);
  const availabilityPalette = await openCommandPalette(page);
  const paletteMap = availabilityPalette.getByTestId(
    'workbench-visibility-command-hide-frisket-geo-view-map',
  );
  await expect(paletteMap).toHaveAttribute('data-availability-status', 'disabled');
  await expect(paletteMap).toHaveAttribute('data-availability-reason', 'data_requirements_unmet');
  // Errors is enabled → it gets a hide command, never a reveal (recovery) entry.
  await expect(
    availabilityPalette.getByTestId(
      'workbench-visibility-command-reveal-frisket-core-panel-errors',
    ),
  ).toHaveCount(0);
  await page.getByLabel('Close command palette').click();

  // Sources re-homed to the Discover panel's Sources tab
  // (workbench-ia-right-edge-v1); the rail's Sources button retired.
  await openDiscoverTab(page, 'Sources');
  await expect(
    page
      .getByTestId('discover-panel')
      .getByTestId('workbench-contribution-frisket-core-panel-sources'),
  ).toBeVisible();

  // Hide a dock tab via the ⌘K palette, then recover it the same way — a real
  // round trip through the visibility mechanism instead of the old hardcoded
  // rail entry.
  const hidePalette = await openCommandPalette(page);
  await hidePalette
    .getByTestId('workbench-visibility-command-hide-frisket-core-panel-errors')
    .click();
  await page.getByLabel('Close command palette').click();
  await expect(page.getByTestId('bottom-dock-tab-errors')).toHaveCount(0);

  const revealPalette = await openCommandPalette(page);
  await revealPalette
    .getByTestId('workbench-visibility-command-reveal-frisket-core-panel-errors')
    .click();
  await page.getByLabel('Close command palette').click();
  await page.getByTestId('bottom-dock-tab-errors').click();
  await expect(page.getByRole('tab', { name: 'Errors' })).toHaveAttribute('aria-selected', 'true');
  await expect(
    page.getByTestId('workbench-contribution-frisket-core-panel-errors'),
  ).toBeVisible();

  // Primary-placement focus (the rail launchers retired with the rail): the
  // dock tab itself selects the History panel. (Projection retired from the
  // dock to a status-bar chip — projection-status-chip-v1 — so it no longer has
  // a dock tab to select here.)
  await page.getByTestId('bottom-dock-tab-history').click();
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-placement-id',
    'history',
  );

  // An unavailable map never grows a repair affordance.
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
  await page.getByRole('tab', { name: 'Jobs' }).click();

  const bottomDock = page.getByTestId('workbench-region-bottomDock');
  // The main view region sits above the bottom dock (the retired sidebar used
  // to anchor this check).
  const mainBox = await page.getByTestId('workbench-region-mainView').boundingBox();
  const bottomDockBox = await bottomDock.boundingBox();
  expect(mainBox).not.toBeNull();
  expect(bottomDockBox).not.toBeNull();
  expect(mainBox!.y + mainBox!.height).toBeLessThanOrEqual(bottomDockBox!.y + 1);
  await expect(bottomDock.getByRole('tablist', { name: 'Operational output' })).toBeVisible();
  await expect(bottomDock.getByRole('tab', { name: 'Jobs' })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  await expect(bottomDock.getByRole('tab', { name: 'Errors' })).toBeVisible();
  // Plugins retired from the dock entirely — see the "no dock Plugins tab" assertion
  // below.
  await expect(bottomDock.getByTestId('bottom-dock-tablist')).toBeVisible();
  await expect(bottomDock.getByRole('tabpanel')).toBeVisible();
  await expect(bottomDock.getByTestId('bottom-dock-panel')).toBeVisible();
  const jobsPanel = bottomDock.getByTestId('workbench-contribution-frisket-core-panel-jobs');
  await expect(jobsPanel).toBeVisible();
  await expect(jobsPanel).toHaveAttribute('data-schema-version', 'frisket.workbench.panel.v1');
  await expect(jobsPanel).toHaveAttribute('data-contribution-id', 'frisket.core.panel.jobs');
  await expect(jobsPanel).toHaveAttribute('data-host', 'bottomDock');
  await expect(jobsPanel).toHaveAttribute('data-mode', 'tab');
  await expect(jobsPanel).toHaveAttribute('data-runtime-component-key', 'core.bottomDock.JobsPanel');
  await expect(jobsPanel).toHaveAttribute('data-required-capabilities', /jobs.list/);
  await expect(jobsPanel.locator('.bottom-dock-split')).toBeVisible();
  const jobsColumn = jobsPanel.getByTestId('bottom-dock-column-jobs-job');
  const jobsColumnHandle = jobsPanel.getByTestId('bottom-dock-column-resize-jobs-job');
  await expect(jobsColumn).toBeVisible();
  await expect(jobsColumnHandle).toBeVisible();
  const initialColumnBox = await jobsColumn.boundingBox();
  const columnHandleBox = await jobsColumnHandle.boundingBox();
  expect(initialColumnBox).not.toBeNull();
  expect(columnHandleBox).not.toBeNull();
  await page.mouse.move(columnHandleBox!.x + columnHandleBox!.width / 2, columnHandleBox!.y + columnHandleBox!.height / 2);
  await page.mouse.down();
  await page.mouse.move(columnHandleBox!.x + 80, columnHandleBox!.y + columnHandleBox!.height / 2);
  await page.mouse.up();
  const widenedColumnBox = await jobsColumn.boundingBox();
  expect(widenedColumnBox).not.toBeNull();
  expect(widenedColumnBox!.width).toBeGreaterThan(initialColumnBox!.width + 40);
  const jobsDetail = jobsPanel.locator('.bottom-dock-detail');
  const jobsSplitHandle = jobsPanel.getByTestId('bottom-dock-split-resize');
  await expect(jobsDetail).toBeVisible();
  await expect(jobsSplitHandle).toBeVisible();
  const initialDetailBox = await jobsDetail.boundingBox();
  const splitHandleBox = await jobsSplitHandle.boundingBox();
  expect(initialDetailBox).not.toBeNull();
  expect(splitHandleBox).not.toBeNull();
  await page.mouse.move(splitHandleBox!.x + splitHandleBox!.width / 2, splitHandleBox!.y + splitHandleBox!.height / 2);
  await page.mouse.down();
  await page.mouse.move(splitHandleBox!.x - 80, splitHandleBox!.y + splitHandleBox!.height / 2);
  await page.mouse.up();
  const widenedDetailBox = await jobsDetail.boundingBox();
  expect(widenedDetailBox).not.toBeNull();
  expect(widenedDetailBox!.width).toBeGreaterThan(initialDetailBox!.width + 40);

  await bottomDock.getByRole('tab', { name: 'Errors' }).click();
  const errorsPanel = bottomDock.getByTestId('workbench-contribution-frisket-core-panel-errors');
  await expect(errorsPanel).toBeVisible();
  await expect(errorsPanel).toHaveAttribute('data-schema-version', 'frisket.workbench.panel.v1');
  await expect(errorsPanel).toHaveAttribute('data-contribution-id', 'frisket.core.panel.errors');
  await expect(errorsPanel).toHaveAttribute('data-host', 'bottomDock');
  await expect(errorsPanel).toHaveAttribute('data-mode', 'tab');
  await expect(errorsPanel).toHaveAttribute('data-runtime-component-key', 'core.bottomDock.ErrorsPanel');
  await expect(errorsPanel).toHaveAttribute('data-required-capabilities', /errors.list/);
  await expect(errorsPanel.locator('.bottom-dock-split')).toBeVisible();
  await expect(errorsPanel.locator('.bottom-dock-detail')).toBeVisible();

  // The preview dock tab (the only first-party kind:view in the dock) retired
  // when it was found to be a dead signpost. Its "a view-kind contribution
  // renders in the dock with capability wiring" witness is dropped rather than
  // relocated: the dock renders view and panel descriptors through the identical
  // BottomDockTabFrame path (no distinct code path), and the product no longer
  // has any dock view — so this removes coverage of a capability that no longer
  // exists, not a live invariant. (The Logs panel left with its dock tab too.)

  // The plugins dock tab retired too — it was a read-only duplicate of the full manager
  // Settings already hosts. See workbench-plugin-runtime-index.spec.ts for
  // the Settings-route "same PluginManager, different mode" coverage.
  await expect(bottomDock.getByRole('tab', { name: 'Plugins' })).toHaveCount(0);

  // 'snippet' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'snippet', 0);
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  // The resident Detail column header renders 'ROW 1' (the overlay drawer's
  // 'Row 1 · sheet' title retired with it).
  await expect(drawer).toContainText(/row 1/i);
  await expect(drawer).toContainText('paving contract');
});
