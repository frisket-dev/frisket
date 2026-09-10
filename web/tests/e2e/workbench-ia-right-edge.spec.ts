import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openCellDrawer,
  openPalette,
  openProject,
  sheetColumns,
  uniqueName,
  type WireColumn,
} from './helpers';

// The right edge contains the resident Inspect Detail column and the Discover
// panel ⇄ rail. Detail and Discover COEXIST (the one-panel rule is
// superseded). The assertions below preserve that contract.

const CITY_CSV = 'city\nAlpha\nBravo\nCharlie\n';

async function openRowDetail(page: Page, columns: WireColumn[], rowIndex: number): Promise<void> {
  // 'city' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'city', rowIndex);
  await expect(page.getByTestId('row-drawer')).toBeVisible();
}

test('row click opens a resident Detail column (not an overlay drawer) with the row fields', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('right-edge-detail'));
  const sheetId = await importCsv(request, pid, 'cities.csv', CITY_CSV);
  const columns = await sheetColumns(request, pid, sheetId);
  await openProject(page, pid, sheetId);

  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
  await openRowDetail(page, columns, 0);

  // The detail lives in the resident Inspect region of the body flex row — it
  // is a column, NOT a fixed overlay drawer (the .drawer overlay is retired).
  const inspect = page.getByTestId('workbench-region-inspect');
  await expect(inspect).toBeVisible();
  await expect(inspect.getByTestId('row-drawer')).toHaveAttribute('data-inspect-detail-column', 'true');
  await expect(page.locator('.drawer')).toHaveCount(0);

  await expect(page.getByTestId('inspect-detail-rownum')).toHaveText('ROW 1');
  await expect(page.getByTestId('row-field-city')).toContainText('Alpha');
});

test('prev/next walks the selection with the panel open; × closes it', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('right-edge-walk'));
  const sheetId = await importCsv(request, pid, 'cities.csv', CITY_CSV);
  const columns = await sheetColumns(request, pid, sheetId);
  await openProject(page, pid, sheetId);

  await openRowDetail(page, columns, 0);
  await expect(page.getByTestId('inspect-detail-rownum')).toHaveText('ROW 1');
  // Prev is disabled at the first row.
  await expect(page.getByTestId('inspect-detail-prev')).toBeDisabled();

  await page.getByTestId('inspect-detail-next').click();
  await expect(page.getByTestId('inspect-detail-rownum')).toHaveText('ROW 2');
  await expect(page.getByTestId('row-field-city')).toContainText('Bravo');
  // Panel stayed open while walking.
  await expect(page.getByTestId('row-drawer')).toBeVisible();

  await page.getByTestId('inspect-detail-prev').click();
  await expect(page.getByTestId('inspect-detail-rownum')).toHaveText('ROW 1');
  await expect(page.getByTestId('row-field-city')).toContainText('Alpha');

  await page.getByTestId('inspect-detail-close').click();
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
});

test('Detail and Discover coexist side by side', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('right-edge-coexist'));
  const sheetId = await importCsv(request, pid, 'cities.csv', CITY_CSV);
  const columns = await sheetColumns(request, pid, sheetId);
  await openProject(page, pid, sheetId);

  // Discover is a resident right-edge panel, open by default.
  await expect(page.getByTestId('discover-panel')).toBeVisible();

  await openRowDetail(page, columns, 0);

  // Opening Detail does NOT close Discover — both render at once.
  await expect(page.getByTestId('workbench-region-inspect')).toBeVisible();
  await expect(page.getByTestId('discover-panel')).toBeVisible();
});

test('Discover tabs switch content; collapse to rail; rail icon re-expands to that tab', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('right-edge-discover'));
  const sheetId = await importCsv(request, pid, 'cities.csv', CITY_CSV);
  await openProject(page, pid, sheetId);

  const panel = page.getByTestId('discover-panel');
  await expect(panel).toBeVisible();
  // Default internal tab is Facets; its user-facing label is Filter.
  const filterTab = page.getByTestId('discover-tab-Facets');
  await expect(filterTab).toHaveAttribute('aria-selected', 'true');
  await expect(filterTab).toHaveText('Filter');
  await expect(
    panel.getByTestId('workbench-contribution-frisket-investigative-panel-friendly-filters'),
  ).toBeVisible();

  // Switch to Sources — the Sources panel contribution renders.
  const sourcesTab = page.getByTestId('discover-tab-Sources');
  if (await sourcesTab.count()) {
    await sourcesTab.click();
  } else {
    await page.getByTestId('discover-tab-overflow').click();
    await page.getByTestId('discover-tab-menu-Sources').click();
  }
  await expect(page.getByTestId('discover-tab-Sources')).toHaveAttribute('aria-selected', 'true');
  await expect(
    panel.getByTestId('workbench-contribution-frisket-core-panel-sources'),
  ).toBeVisible();

  // Collapse to the 48px rail: the panel body is hidden, the rail shows.
  await page.getByTestId('discover-collapse').click();
  await expect(page.getByTestId('discover-panel')).toHaveCount(0);
  await expect(page.getByTestId('discover-rail')).toBeVisible();

  // Clicking a rail icon re-expands to THAT tab.
  await page.getByTestId('discover-rail-icon-Watches').click();
  await expect(page.getByTestId('discover-panel')).toBeVisible();
  await expect(page.getByTestId('discover-tab-Watches')).toHaveAttribute('aria-selected', 'true');
});

test('resident Discover controls collapse and re-expand the panel', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('right-edge-resident-controls'));
  const sheetId = await importCsv(request, pid, 'cities.csv', CITY_CSV);
  await openProject(page, pid, sheetId);

  await expect(page.getByTestId('discover-panel')).toBeVisible();

  // Discover owns its visibility controls at the right edge; it is not a
  // compact-only Act category or command.
  await page.getByTestId('discover-collapse').click();
  await expect(page.getByTestId('discover-panel')).toHaveCount(0);
  await expect(page.getByTestId('discover-rail')).toBeVisible();

  const filterRailButton = page.getByTestId('discover-rail-icon-Facets');
  await expect(filterRailButton).toHaveAttribute('aria-label', 'Open Filter');
  await filterRailButton.click();
  await expect(page.getByTestId('discover-panel')).toBeVisible();
  await expect(page.getByTestId('discover-tab-Facets')).toHaveAttribute('aria-selected', 'true');
});

test('Detail + Discover widths persist across reload', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('right-edge-widths'));
  const sheetId = await importCsv(request, pid, 'cities.csv', CITY_CSV);
  const columns = await sheetColumns(request, pid, sheetId);
  await openProject(page, pid, sheetId);

  // Widen the Discover panel via its keyboard-resizable seam.
  const beforeDiscover = (await page.getByTestId('discover-panel').boundingBox())!.width;
  await page.getByTestId('discover-seam').focus();
  for (let i = 0; i < 4; i++) await page.keyboard.press('ArrowLeft');
  const widenedDiscover = (await page.getByTestId('discover-panel').boundingBox())!.width;
  expect(widenedDiscover).toBeGreaterThan(beforeDiscover);

  // Widen the Detail column via its seam.
  await openRowDetail(page, columns, 0);
  const beforeDetail = (await page.getByTestId('row-drawer').boundingBox())!.width;
  await page.getByTestId('inspect-detail-seam').focus();
  for (let i = 0; i < 4; i++) await page.keyboard.press('ArrowLeft');
  const widenedDetail = (await page.getByTestId('row-drawer').boundingBox())!.width;
  expect(widenedDetail).toBeGreaterThan(beforeDetail);

  // Reload: both widths are restored from localStorage.
  await openProject(page, pid, sheetId);
  await openRowDetail(page, columns, 0);
  expect(Math.abs((await page.getByTestId('discover-panel').boundingBox())!.width - widenedDiscover)).toBeLessThan(2);
  expect(Math.abs((await page.getByTestId('row-drawer').boundingBox())!.width - widenedDetail)).toBeLessThan(2);
});

test('the left sidebar is retired; Search + Copilot re-homed to the palette + popover', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('right-edge-sidebar'));
  const sheetId = await importCsv(request, pid, 'cities.csv', CITY_CSV);
  await openProject(page, pid, sheetId);

  // No left sidebar region survives (workbench-ia-focus-v1).
  await expect(page.getByTestId('workbench-region-leftSidebar')).toHaveCount(0);
  await expect(page.locator('.sidebar')).toHaveCount(0);

  // Search re-homed to the ⌘K palette's SEARCH section.
  const palette = await openPalette(page);
  await expect(palette.getByTestId('command-palette-input')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(palette).toBeHidden();

  // Copilot re-homed to the ✧ Focus popover.
  await page.getByTestId('chrome-copilot-toggle').click();
  await expect(page.getByTestId('copilot-popover').getByTestId('copilot-panel')).toBeVisible();
});

test('the » overflow menu OPENS and switches to a hidden tab (regression: clipped by overflow:hidden)', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('discover-overflow'));
  const sheetId = await importCsv(request, pid, 'a.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  // At the default 290px width, five tabs overflow — the » button renders.
  const overflowBtn = page.getByTestId('discover-tab-overflow');
  await expect(overflowBtn).toBeVisible();

  // The menu must be genuinely interactable (a clipped menu makes this click
  // time out — Playwright requires a visible point to click).
  await overflowBtn.click();
  const menu = page.getByTestId('discover-tab-overflow-menu');
  await expect(menu).toBeVisible();
  const firstItem = menu.locator('.discover-overflow-item').first();
  const label = (await firstItem.textContent())?.trim() ?? '';
  await firstItem.click();
  await expect(page.getByTestId('discover-body')).toHaveAttribute('data-active-tab', label);
  await expect(menu).toHaveCount(0);
});

test('a long unbreakable sheet name ellipsizes in the Detail header — one line, × reachable', async ({
  page,
  request,
}) => {
  const longName = `youtube-playlist-watch-v-EkIb0QMWPbE-list-PL_Aetb7e8ejzekqszBPUi-${'y'.repeat(40)}`;
  const pid = await createProject(request, uniqueName('detail-ovf'));
  const sheetId = await importCsv(request, pid, `${longName}.csv`, CITY_CSV);
  const columns = await sheetColumns(request, pid, sheetId);
  await openProject(page, pid, sheetId);
  await openRowDetail(page, columns, 0);

  const header = page.locator('.inspect-detail-header');
  await expect(header).toBeVisible();

  // Single-line header: its height stays that of one row of chips (< 50px),
  // and the sheet-name span truncates with the full name in its title.
  const headerBox = await header.boundingBox();
  expect(headerBox!.height).toBeLessThan(50);
  const nameSpan = header.locator('.inspect-detail-sheet');
  await expect(nameSpan).toHaveAttribute('title', new RegExp('youtube-playlist-watch'));
  expect(await nameSpan.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);

  // × stays inside the column and works without any horizontal scrolling.
  const close = page.getByTestId('inspect-detail-close');
  const closeBox = await close.boundingBox();
  const colBox = await page.locator('.inspect-detail-column').boundingBox();
  expect(closeBox!.x + closeBox!.width).toBeLessThanOrEqual(colBox!.x + colBox!.width + 1);
  await close.click();
  await expect(page.locator('.inspect-detail-column')).toHaveCount(0);
});
