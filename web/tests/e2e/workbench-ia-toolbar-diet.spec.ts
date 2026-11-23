// The sheet header row: sheet name/count · view switcher · split hint · a
// selection-gated Delete-N · Add ▾ (Add row, Add column…) · Filter with its
// active-count badge · Wrap. Search, sort, limit, facets and views live in the
// Filter sidebar; Receipts live in the project menu. The Window menu stays
// retired; hide/reveal works through the ⌘K palette.

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
  'toolbar-overflow-button',
  'row-height-select',
  'open-views-submenu',
  'grid-add-column-button',
];

test('the sheet header row renders only the kept controls; removed ones are gone', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('toolbar-diet-kept'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'people.csv',
    'name,city\nAda,Syracuse\nGrace,Albany\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await expect(page.getByTestId('sheet-stats')).toHaveText('2 rows · 2 columns');
  await expect(page.getByTestId('view-switcher')).toBeVisible();
  await expect(page.getByTestId('add-menu-button')).toBeVisible();
  await expect(page.getByTestId('filter-sidebar-toggle')).toBeVisible();
  await expect(page.getByTestId('filter-badge')).toHaveCount(0);
  await expect(page.getByTestId('toggle-wrap')).toBeVisible();
  await expect(page.getByTestId('toolbar-toggle-discover')).toHaveCount(0);
  // Nothing is active, so there is no chip strip.
  await expect(page.getByTestId('row-filter-chips')).toHaveCount(0);

  // Delete-N is selection-gated: absent (not merely disabled) with no selection.
  await expect(page.getByTestId('delete-rows-button')).toHaveCount(0);

  for (const testid of REMOVED_TOOLBAR_TESTIDS) {
    await expect(page.getByTestId(testid), `${testid} should be removed`).toHaveCount(0);
  }
  // Receipts are in the project menu, not the sheet toolbar.
  await expect(page.getByTestId('open-provenance-manifest')).toHaveCount(0);
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

test('Wrap, Add ▾, Filter and the project menu each work from the keyboard and pointer', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('toolbar-diet-controls'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'people.csv',
    'name,city\nAda,Syracuse\nGrace,Albany\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const wrap = page.getByTestId('toggle-wrap');
  const wrapBefore = await wrap.getAttribute('aria-pressed');
  await wrap.click();
  await expect(wrap).not.toHaveAttribute('aria-pressed', wrapBefore ?? '');
  await expect(wrap).toHaveAttribute('title', wrapBefore === 'true' ? 'Wrap text: off' : 'Wrap text: on');

  // Add ▾ opens a menu whose first item takes focus; Escape returns focus.
  const add = page.getByTestId('add-menu-button');
  await add.click();
  await expect(page.getByTestId('add-menu')).toBeVisible();
  await expect(page.getByTestId('add-row-button')).toBeFocused();
  await expect(page.getByTestId('add-column-button')).toHaveText('Add column…');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('add-menu')).toHaveCount(0);
  await expect(add).toBeFocused();

  // Add column… hands off to the add-column popover.
  await add.click();
  await page.getByTestId('add-column-button').click();
  await expect(page.getByTestId('grid-add-column-popover')).toBeVisible();
  await page.getByTestId('grid-add-column-cancel').click();

  // Filter opens the sidebar on its Filter tab; Escape closes it and returns focus.
  const filter = page.getByTestId('filter-sidebar-toggle');
  await filter.click();
  await expect(filter).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByTestId('filter-sidebar-tab-filter')).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('filter-sidebar')).toHaveCount(0);
  await expect(filter).toBeFocused();

  // Receipts open from the project menu.
  await page.getByTestId('switch-project').click();
  await page.getByTestId('open-provenance-manifest').click();
  await expect(page.getByTestId('provenance-manifest')).toBeVisible();
});

test('caret menu carries Filter…, Search in…, sort and Columns', async ({ page }) => {
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

  await clickHeaderMenu(page, columns, 'city');
  await page.getByTestId('header-menu-column-settings').click();
  await expect(page.getByTestId('column-drawer')).toBeVisible();
  await page.getByTestId('column-drawer').getByLabel('Close drawer').click();

  // Filter… opens the sidebar on this column's facet, marked as focused.
  await clickHeaderMenu(page, columns, 'city');
  await expect(page.getByTestId('header-menu-filter-sidebar')).toHaveCount(0);
  await expect(page.getByTestId('header-menu-advanced-sort')).toHaveCount(0);
  await expect(page.getByTestId('header-menu-clear-sort')).toHaveCount(0);
  await expect(page.getByTestId('header-menu-clear-filter')).toHaveCount(0);
  await page.getByTestId('header-menu-filter').click();
  await expect(page.getByTestId('filter-sidebar')).toBeVisible();
  await expect(page.getByTestId('friendly-facet-city')).toHaveAttribute('data-focused', 'true');

  // Sorting from the caret shows a sort chip and a Clear sort item.
  await clickHeaderMenu(page, columns, 'city');
  await page.getByTestId('header-menu-sort-desc').click();
  await expect(page.getByTestId('row-filter-chip-sort')).toContainText('Sorted by city ↓');
  await clickHeaderMenu(page, columns, 'city');
  await page.getByTestId('header-menu-clear-sort').click();
  await expect(page.getByTestId('row-filter-chip-sort')).toHaveCount(0);
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
