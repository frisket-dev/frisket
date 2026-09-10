// RED-FIRST (authored 2026-06-13): opening a row from a specific grid cell
// should focus the corresponding field in the row detail drawer.

import { expect, test } from '@playwright/test';
import {
  clickCell,
  createProject,
  importCsv,
  openCellDrawer,
  sheetColumns,
  uniqueName,
} from './helpers';

test('row drawer scrolls to and marks the activated cell field', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-row-focus'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'country,search_results,summary\n' +
      '"Germany","[{""title"":""One"",""url"":""https://example.com"",""snippet"":""hit""}]","US tariffs are a concern"\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await clickCell(page, columns, 'search_results', 0);
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
  // Activate the selected cell to open the resident Detail column. (Enter is
  // the stable activation gesture; double-click on an overlay-editable JSON
  // cell routes to the grid's inline editor, not the Detail column.)
  await page.keyboard.press('Enter');

  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  const selectedField = page.getByTestId('row-field-search_results');
  await expect(selectedField).toBeVisible();
  await expect(selectedField).toHaveAttribute('data-selected-cell', 'true');
  await expect(selectedField).toBeInViewport();
  await expect(drawer.getByTestId('json-mini-table')).toBeVisible();
});

test('keyboard cursor movement updates the row drawer selected field', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-row-keyboard-focus'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'country,search_results,summary\n' +
      '"Germany","[{""title"":""One"",""url"":""https://example.com"",""snippet"":""hit""}]","US tariffs are a concern"\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
  // 'country' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'country', 0);
  await expect(page.getByTestId('row-field-country')).toHaveAttribute(
    'data-selected-cell',
    'true',
  );

  await page.keyboard.press('ArrowRight');

  const nextField = page.getByTestId('row-field-search_results');
  await expect(nextField).toHaveAttribute('data-selected-cell', 'true');
  await expect(nextField).toBeInViewport();
});
