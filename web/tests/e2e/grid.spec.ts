// The sheet grid + row/column drawers, against the seeded "Local stories"
// project (read-only — nothing here mutates seed data).
//
// The grid itself is a canvas, so cell-level assertions go through the row
// drawer (real DOM for the same data) and coordinate clicks (helpers.ts).

import { expect, test } from '@playwright/test';
import {
  addRow,
  clickCell,
  clickHeader,
  createProject,
  importCsv,
  listSheets,
  openAction,
  openCellDrawer,
  openProject,
  projectIdByName,
  sheetColumns,
  type WireColumn,
  uniqueName,
} from './helpers';

let pid: string;
let columns: WireColumn[];

test.beforeEach(async ({ page }) => {
  pid = await projectIdByName(page.request, 'Local stories');
  const sheets = await listSheets(page.request, pid);
  columns = await sheetColumns(page.request, pid, sheets[0].id);
  await openProject(page, pid);
});

test('grid renders the seeded rows and AI columns', async ({ page }) => {
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('sheet-stats')).toHaveText('8 rows · 6 columns');
  // AI columns (beat, newsworthiness, …) are listed — with the ⚡ marker —
  // in the recipe form's Save-to combobox.
  await openAction(page, 'map.classify');
  await expect(page.getByTestId('new-column-name')).toBeVisible();
  const options = page.locator('#new-column-name-options');
  await expect(options.locator('option[value="beat"]')).toHaveCount(1);
  await expect(options.locator('option[value="snippet"]')).toHaveCount(1);
});

test('cell activation opens the drawer with justification + provenance', async ({ page }) => {
  await clickCell(page, columns, 'snippet', 0);
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
  // 'snippet' is the plain non-AI source column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await page.getByTestId('cell-details-float').click();
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText('ROW 1'); // resident Detail column header
  await expect(drawer).toContainText('paving contract'); // the source cell
  // Each AI cell carries provenance (recipe/model/confidence) + justification.
  expect(await drawer.getByTestId('cell-provenance').count()).toBeGreaterThan(0);
  const justification = drawer.getByTestId('prov-justification').first();
  await expect(justification).not.toBeEmpty();
  // Note: provenance model/recipe names are only known for runs started in
  // this browser session (src/api/real.ts colRunInfo) — for seeded runs the
  // block still renders, with confidence + justification carrying the weight.
  await expect(drawer.getByTestId('cell-provenance').first()).toContainText('confidence');
});

test('AI column header opens the column drawer', async ({ page }) => {
  await clickHeader(page, columns, 'beat');
  const drawer = page.getByTestId('column-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText('beat');
  await expect(drawer).toContainText('Classify rows');
  await expect(drawer).not.toContainText(/\brecipes?\b/i);
  await expect(drawer.getByTestId('column-prompt')).toContainText('local news story');
  await expect(drawer).toContainText('gemini'); // authoritative model via /columns/{id}/runs
  await expect(drawer.getByTestId('column-versions').locator('li')).toHaveCount(1);
});

test('json array cell renders as a mini table in the row drawer', async ({ page }) => {
  // "Tariff impacts" carries a json column (search_results from web_search);
  // in the grid it draws as a count badge, in the drawer as a mini table.
  const tariff = await projectIdByName(page.request, 'Tariff impacts');
  const sheets = await listSheets(page.request, tariff);
  const cols = await sheetColumns(page.request, tariff, sheets[0].id);
  expect(cols.find((c) => c.name === 'search_results')?.type).toBe('json');
  await openProject(page, tariff);
  // 'country' is a plain non-AI source column (now in-place editable —
  // grid-in-place-edit-v1), so double-click edits it instead of opening the
  // drawer; open via the floating icon instead.
  await openCellDrawer(page, cols, 'country', 0);
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId('json-mini-table')).toBeVisible();
  expect(await drawer.getByTestId('json-mini-table').locator('tbody tr').count()).toBeGreaterThan(0);
});

test('link cells with unsafe schemes render inert in the row drawer', async ({ page }) => {
  const testPid = await createProject(page.request, uniqueName('link-xss'));
  const sheetId = await importCsv(
    page.request,
    testPid,
    'links.csv',
    'url\nhttps://example.com/safe\n',
  );
  await addRow(page.request, testPid, sheetId, {
    url: 'javascript:window.__row_drawer_link_xss=1',
  });

  const linkColumns = await sheetColumns(page.request, testPid, sheetId);
  expect(linkColumns.find((c) => c.name === 'url')?.type).toBe('link');
  await openProject(page, testPid, sheetId);

  await clickCell(page, linkColumns, 'url', 0);
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
  await page.keyboard.press('Enter');
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer.locator('a.row-field-link')).toHaveAttribute('href', 'https://example.com/safe');
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();

  await clickCell(page, linkColumns, 'url', 1);
  await expect(drawer).toBeHidden();
  await page.keyboard.press('Enter');
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText('javascript:window.__row_drawer_link_xss=1');
  await expect(drawer.locator('a.row-field-link')).toHaveCount(0);
  expect(
    await page.evaluate(() => (window as unknown as { __row_drawer_link_xss?: number }).__row_drawer_link_xss),
  ).toBeFalsy();
});

test('detail column has a resize seam and closes on Escape', async ({ page }) => {
  await openCellDrawer(page, columns, 'snippet', 1);
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  // The resident Detail column's left seam (workbench-ia-right-edge-v1).
  await expect(page.getByTestId('inspect-detail-seam')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();
});
