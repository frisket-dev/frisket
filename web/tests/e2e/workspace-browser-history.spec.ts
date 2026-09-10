// Browser history should restore workspace state, not just update URL text:
// sheet/subview switches, row/column drawers, review overlay, and existing
// action deep links all round-trip through Back/Forward.

import { expect, test } from '@playwright/test';
import {
  clickHeader,
  createProject,
  importCsv,
  openCellDrawer,
  openProject,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';

const routeRe = (path: string) => new RegExp(`${path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:\\?|$)`);

test('browser back/forward restores sheets, drawers, review, and action deep links', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-browser-history'));
  const firstSheetId = await importCsv(
    page.request,
    pid,
    'cities.csv',
    'city,status\nAlbany,open\nBuffalo,closed\n',
  );
  const secondSheetId = await importCsv(
    page.request,
    pid,
    'countries.csv',
    'country\nGermany\nSpain\n',
  );
  const columns = await sheetColumns(page.request, pid, firstSheetId);
  const rows = await sheetData(page.request, pid, firstSheetId, 0, 5);
  const city = columns.find((column) => column.name === 'city');
  const status = columns.find((column) => column.name === 'status');
  expect(city).toBeTruthy();
  expect(status).toBeTruthy();
  const firstRow = rows.rows[0];
  expect(firstRow).toBeTruthy();

  await openProject(page, pid, firstSheetId);
  await expect(page.getByTestId(`workbench-mainView-tab-${firstSheetId}`)).toHaveClass(/active/);

  await page.getByTestId(`workbench-mainView-tab-${secondSheetId}`).click();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${secondSheetId}`));
  await expect(page.getByTestId(`workbench-mainView-tab-${secondSheetId}`)).toHaveClass(/active/);

  await page.goBack();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${firstSheetId}`));
  await expect(page.getByTestId(`workbench-mainView-tab-${firstSheetId}`)).toHaveClass(/active/);

  await page.goForward();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${secondSheetId}`));
  await expect(page.getByTestId(`workbench-mainView-tab-${secondSheetId}`)).toHaveClass(/active/);

  await page.goBack();
  await expect(page.getByTestId(`workbench-mainView-tab-${firstSheetId}`)).toHaveClass(/active/);

  // 'city' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'city', 0);
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('row-drawer')).toContainText('Albany');
  await expect(page).toHaveURL(
    routeRe(`/p/${pid}/s/${firstSheetId}/row/${firstRow.id}/column/${city!.id}`),
  );

  await page.goBack();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${firstSheetId}`));
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);

  await page.goForward();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('row-drawer')).toContainText('Albany');

  await page.goBack();
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);

  await clickHeader(page, columns, 'status');
  await expect(page.getByTestId('column-drawer')).toBeVisible();
  await expect(page.getByTestId('column-drawer')).toContainText('status');
  await expect(page).toHaveURL(
    routeRe(`/p/${pid}/s/${firstSheetId}/column/${status!.id}`),
  );

  await page.goBack();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${firstSheetId}`));
  await expect(page.getByTestId('column-drawer')).toHaveCount(0);

  await page.goForward();
  await expect(page.getByTestId('column-drawer')).toBeVisible();

  await page.goBack();
  await expect(page.getByTestId('column-drawer')).toHaveCount(0);

  await page.getByTestId('review-queue-button').click();
  await expect(page.getByTestId('review-queue')).toBeVisible();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${firstSheetId}/review`));

  await page.goBack();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${firstSheetId}`));
  await expect(page.getByTestId('review-queue')).toHaveCount(0);

  await page.goForward();
  await expect(page.getByTestId('review-queue')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('review-queue')).toHaveCount(0);

  await page.goto(`/p/${pid}/s/${firstSheetId}/action/enrich.geocode`);
  await expect(page.getByTestId('action-form')).toBeVisible();
  await expect(page.getByTestId('action-form-title')).toContainText('Geocode');

  await page.goBack();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${firstSheetId}`));
  await expect(page.getByTestId('action-form')).toHaveCount(0);
});
