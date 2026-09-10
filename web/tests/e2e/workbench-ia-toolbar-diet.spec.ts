// Toolbar diet + Window-menu retirement (workbench-ia-toolbar-diet-v1).
//
// The sheet toolbar keeps ONLY: sheet name/count · view switcher · split hint ·
// add-row · a selection-gated Delete-N (rendered only when rows are selected) ·
// a spacer · a Discover toggle · a ⋯ overflow menu. Everything else is either
// deleted outright (inline filter, grid filter/sort buttons, cluster toggle,
// open-map/graph buttons, column-settings select, action-panel toggle) or
// re-homed:
//   - filter/sort panels + column settings → the column ▾ caret menu (advanced
//     Filter/Sort rows + the existing settings row; the panels survive)
//   - wrap text · row height · saved views · provenance → the ⋯ overflow menu
//     with the same testids, relocated.
// The Window menu retires fully; hide/reveal stays through the ⌘K palette.

import { expect, test } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  importCsv,
  openProject,
  selectRow,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';

const REMOVED_TOOLBAR_TESTIDS = [
  'inline-filter-toggle',
  'grid-filter-button',
  'grid-sort-button',
  'cluster-toggle',
  'open-map-button',
  'open-map-select',
  'open-graph-button',
  'column-settings-select',
  'toggle-action-panel',
];

// Controls that moved INTO the ⋯ overflow: absent from the main strip while the
// overflow is closed, present (same testids) once it opens.
const OVERFLOW_HOSTED_TESTIDS = [
  'row-height-select',
  'open-saved-views-submenu',
  'open-provenance-manifest',
];

test('dieted toolbar renders only the kept controls; removed ones are gone', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('toolbar-diet-kept'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'people.csv',
    'name,city\nAda,Syracuse\nGrace,Albany\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Kept controls.
  await expect(page.getByTestId('sheet-stats')).toBeVisible();
  await expect(page.getByTestId('view-switcher')).toBeVisible();
  await expect(page.getByTestId('add-row-button')).toBeVisible();
  // Wrap text earned toolbar rank; Discover has NO toolbar toggle — the
  // rail's ‹ is the affordance.
  await expect(page.getByTestId('toggle-wrap')).toBeVisible();
  await expect(page.getByTestId('toolbar-toggle-discover')).toHaveCount(0);
  await expect(page.getByTestId('toolbar-overflow-button')).toBeVisible();

  // Delete-N is selection-gated: absent (not merely disabled) with no selection.
  await expect(page.getByTestId('delete-rows-button')).toHaveCount(0);

  // Every removed control is gone entirely.
  for (const testid of REMOVED_TOOLBAR_TESTIDS) {
    await expect(page.getByTestId(testid), `${testid} should be removed`).toHaveCount(0);
  }

  // Re-homed controls are absent from the main strip until the overflow opens.
  for (const testid of OVERFLOW_HOSTED_TESTIDS) {
    await expect(page.getByTestId(testid), `${testid} should be hidden in overflow`).toHaveCount(0);
  }
});

test('Delete-N appears only with a selection and deletes the selected rows', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('toolbar-diet-delete'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'people.csv',
    'name\nAda\nGrace\nKatherine\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('sheet-stats')).toHaveText('3 rows · 1 column');

  await expect(page.getByTestId('delete-rows-button')).toHaveCount(0);

  await selectRow(page, 0);
  await selectRow(page, 2);
  const deleteBtn = page.getByTestId('delete-rows-button');
  await expect(deleteBtn).toBeVisible();
  // Icon-only (tab-delete-icon-buttons-v1): the count lives in aria-label/title,
  // not visible text.
  await expect(deleteBtn).toHaveAttribute('title', 'Delete 2 selected row(s)');

  await deleteBtn.click();
  await page.getByTestId('delete-rows-confirm').click();
  await expect(page.getByTestId('sheet-stats')).toHaveText('1 row · 1 column');
  await expect.poll(async () => (await sheetData(page.request, pid, sheetId)).total).toBe(1);

  // Selection cleared → Delete-N gone again.
  await expect(page.getByTestId('delete-rows-button')).toHaveCount(0);
});

test('⋯ overflow hosts wrap / row-height / views / provenance and each works', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('toolbar-diet-overflow'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'people.csv',
    'name,city\nAda,Syracuse\nGrace,Albany\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const overflowMenu = page.getByTestId('toolbar-overflow-menu');
  // Idempotent: the four items differ in whether selecting them dismisses the
  // menu (views/provenance close it; wrap/row-height leave it open).
  const openOverflow = async () => {
    if ((await overflowMenu.count()) === 0) {
      await page.getByTestId('toolbar-overflow-button').click();
    }
    await expect(overflowMenu).toBeVisible();
  };

  // All four affordances live inside the overflow.
  await openOverflow();
  for (const testid of OVERFLOW_HOSTED_TESTIDS) {
    await expect(page.getByTestId(testid), `${testid} in overflow`).toBeVisible();
  }

  // Wrap text toggles its pressed state from the TOOLBAR (not the overflow).
  const wrap = page.getByTestId('toggle-wrap');
  const wrapBefore = await wrap.getAttribute('aria-pressed');
  await wrap.click();
  await expect(wrap).not.toHaveAttribute('aria-pressed', wrapBefore ?? '');

  // Row height keeps the Frisket select surface above the overflow popover and
  // applies a real pointer selection (selectOption would bypass that UI).
  await openOverflow();
  const rowHeightSelect = page.getByTestId('row-height-select');
  await rowHeightSelect.click();
  const rowHeightMenu = page.getByTestId('row-height-select-menu');
  await expect(rowHeightMenu).toHaveCount(1);
  await expect(rowHeightMenu).toBeVisible();
  await expect(overflowMenu).toBeVisible();
  await expect(rowHeightMenu).toHaveCSS('position', 'fixed');
  await expect.poll(async () => rowHeightMenu.evaluate((menu) => menu.matches(':popover-open'))).toBe(true);
  const compact = rowHeightMenu.getByRole('option', { name: 'Compact' });
  await expect.poll(async () => compact.evaluate((option) => {
    const rect = option.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return hit !== null && option.contains(hit);
  })).toBe(true);
  await rowHeightMenu.getByRole('option', { name: 'Roomy' }).click();
  await expect(rowHeightSelect).toHaveValue('48');
  await expect(rowHeightMenu).toHaveCount(0);
  await expect(overflowMenu).toBeVisible();

  // Saved views opens the views panel.
  await openOverflow();
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  await expect(page.getByTestId('views-panel')).toBeVisible();

  // Provenance opens the manifest.
  await openOverflow();
  await page.getByTestId('open-provenance-manifest').click();
  await expect(page.getByTestId('provenance-manifest')).toBeVisible();
});

test('caret menu carries friendly Filter, advanced Sort, and Columns', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('toolbar-diet-caret'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'people.csv',
    'name,city\nAda,Syracuse\nGrace,Albany\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Columns → the surviving column-settings drawer. Done first, while the grid
  // is clean (the filter/sort panels below push the header down).
  await clickHeaderMenu(page, columns, 'city');
  await page.getByTestId('header-menu-column-settings').click();
  await expect(page.getByTestId('column-drawer')).toBeVisible();
  await page.getByTestId('column-drawer').getByLabel('Close drawer').click();

  // Rich filter → the Facets sidebar, now filtering's single home.
  await clickHeaderMenu(page, columns, 'city');
  await page.getByTestId('header-menu-filter-sidebar').click();
  await expect(page.getByTestId('discover-panel')).toBeVisible();
  await expect(page.getByTestId('friendly-facet-city')).toBeVisible();

  // Advanced sort → the surviving grid-sort-panel.
  await clickHeaderMenu(page, columns, 'city');
  await page.getByTestId('header-menu-advanced-sort').click();
  await expect(page.getByTestId('grid-sort-panel')).toBeVisible();
});

const GRAPH_SLUG = 'frisket-investigative-view-graph-neighborhood';

test('Window trigger is gone; the ⌘K palette hides and reveals a contribution end-to-end', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('toolbar-diet-window'));
  const sheetId = await importCsv(page.request, pid, 'routes.csv', 'title\nRoute row\n');
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // The Window menu trigger + its machinery are fully retired.
  await expect(page.getByTestId('workbench-window-menu-button')).toHaveCount(0);
  await expect(page.getByTestId('workbench-window-menu')).toHaveCount(0);
  await expect(page.getByTestId('workbench-workspace-presets')).toHaveCount(0);
  await expect(page.getByTestId('workbench-mainView-command-target')).toHaveCount(0);

  const graphLayoutItem = page
    .getByTestId('workbench-resolved-layout-region-mainView')
    .getByTestId(`workbench-resolved-layout-item-${GRAPH_SLUG}`);

  const openPalette = async () => {
    await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
    const palette = page.getByTestId('workbench-region-commandPalette');
    await expect(palette).toBeVisible();
    return palette;
  };

  // ⌘K palette hide → gone from the mainView region.
  let palette = await openPalette();
  await palette.getByTestId(`workbench-visibility-command-hide-${GRAPH_SLUG}`).click();
  await page.getByLabel('Close command palette').click();
  await expect(graphLayoutItem).toHaveAttribute('data-status', 'hidden');
  await expect(graphLayoutItem).toHaveAttribute('data-reason', 'hidden_by_profile');

  // ⌘K palette reveal → back in the region (full round-trip).
  palette = await openPalette();
  await palette.getByTestId(`workbench-visibility-command-reveal-${GRAPH_SLUG}`).click();
  await page.getByLabel('Close command palette').click();
  await expect(graphLayoutItem).not.toHaveAttribute('data-status', 'hidden');

  // The retired move-between-hosts command is gone from the palette.
  palette = await openPalette();
  await expect(
    palette.getByTestId('workbench-command-frisket-core-command-move-map-mainview'),
  ).toHaveCount(0);
});
