// geo_point cells: backend type registry advertises renderer='map-pin', and
// the frontend renders map details in the row drawer without raw JSON syntax.

import { expect, test } from '@playwright/test';
import { clickCell, openProject, seedGeoSheet, sheetColumns } from './helpers';

test('geo_point renders as map detail in the row drawer', async ({ page }) => {
  const registry = await page.request.get('/api/column-types');
  expect(registry.ok()).toBeTruthy();
  const types = await registry.json() as Array<{ name: string; presentation?: { renderer?: string } }>;
  expect(types.find((t) => t.name === 'geo_point')?.presentation?.renderer).toBe('map-pin');

  const { pid, sheetId } = await seedGeoSheet(page, {
    namePrefix: 'geo-point',
    point: { lat: 48.8584, lon: 2.2945 },
  });

  const columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.find((c) => c.name === 'point')?.type).toBe('geo_point');

  await openProject(page, pid, sheetId);
  await clickCell(page, columns, 'point', 0);
  await page.keyboard.press('Enter');
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId('geo-point-value')).toBeVisible();
  await expect(drawer.getByTestId('geo-point-coordinates')).toContainText('48.85840, 2.29450');
  await expect(drawer.getByTestId('geo-point-map-link')).toHaveAttribute(
    'href',
    /openstreetmap\.org\/\?mlat=48\.8584&mlon=2\.2945/,
  );
  await expect(drawer).not.toContainText('{"lat"');
});
