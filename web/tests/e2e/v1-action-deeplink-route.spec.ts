import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  uniqueName,
} from './helpers';

const routeRe = (path: string) => new RegExp(`${path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:\\?|$)`);

test('public action deep links open forms and old recipe paths do not open actions', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-v1-action-route'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'addresses.csv',
    'address,state,country\n"1600 Pennsylvania Ave NW",DC,US\n',
  );

  await page.goto(`/p/${pid}/s/${sheetId}/action/enrich.geocode`);
  await expect(page.getByTestId('generated-action-form')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('action-form-title')).toContainText('Geocode');
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${sheetId}/action/enrich.geocode`));
  expect(page.url()).not.toContain('/recipe/');

  await page.goto(`/p/${pid}/s/${sheetId}/recipe/enrich.geocode`);
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${sheetId}`));
  await expect(page.getByTestId('generated-action-form')).toBeHidden();
  expect(page.url()).not.toContain('/recipe/');

  await page.goBack();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${sheetId}/action/enrich.geocode`));
  await expect(page.getByTestId('generated-action-form')).toBeVisible({ timeout: 20_000 });
  expect(page.url()).not.toContain('/recipe/');
  await page.goForward();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${sheetId}`));
  await expect(page.getByTestId('generated-action-form')).toBeHidden();
  expect(page.url()).not.toContain('/recipe/');
});

test('missing Census geo prerequisite opens geocode through the public action route', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-v1-action-prereq'));
  await importCsv(page.request, pid, 'rows.csv', 'address\n"1600 Pennsylvania Ave NW"\n');

  await page.goto(`/p/${pid}`);
  await openAction(page, 'enrich.census_demographics');
  await expect(page.getByTestId('generated-action-form')).toBeVisible();
  await page.getByRole('button', { name: 'Run Geocode first' }).click();
  await expect(page).toHaveURL(/\/action\/enrich\.geocode(?:\?|$)/);
  expect(page.url()).not.toContain('/recipe/');
  await expect(page.getByTestId('action-form-title')).toContainText(/geocode/i);
});
