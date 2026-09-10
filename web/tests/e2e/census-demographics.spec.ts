import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

test('census demographics asks for a geo_point source and links to geocode when missing', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-census-no-point'));
  await importCsv(page.request, pid, 'rows.csv', 'address\n"1600 Pennsylvania Ave NW"\n');

  await page.goto(`/p/${pid}`);
  await openAction(page, 'enrich.census_demographics');
  await expect(page.getByTestId('action-credential-gate')).toContainText('CENSUS_API_KEY');
  await expect(page.getByTestId('census-no-geo-point')).toContainText(
    'No compatible geo_point columns.',
  );
  await expect(page.getByTestId('generated-action-run')).toBeDisabled();
  const geocodeHandoff = page.getByRole('button', { name: 'Run Geocode first' });
  await expect(geocodeHandoff).toBeEnabled();
  await geocodeHandoff.click();
  await expect(page).toHaveURL(/\/action\/enrich\.geocode(?:\?|$)/);
  await expect(page.getByTestId('action-form-title')).toContainText(/geocode/i);
});

test('census demographics filters source choices to geo_point columns', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-census-point'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'address,point\n"1600 Pennsylvania Ave NW",\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((c) => c.name === 'point');
  expect(point).toBeTruthy();
  await setColumnType(page.request, pid, point!.id, 'geo_point');

  await page.goto(`/p/${pid}`);
  await openAction(page, 'enrich.census_demographics');
  const select = page.getByRole('combobox', { name: 'Geo point column' });
  await expect(select).toBeVisible();
  await expect(select).toHaveValue('point');
  await expect(select.locator('option')).toHaveCount(1);
  await expect(page.getByTestId('field-geography')).toHaveValue('tract');
  await page.getByTestId('field-include_moe').check();
  await expect(page.getByTestId('cost-estimate')).toContainText('Census ACS');
});
