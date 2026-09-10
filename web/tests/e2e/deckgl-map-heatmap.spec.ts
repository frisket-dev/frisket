// Points / Heatmap density toggle on the deck.gl map.

import { expect, test } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  mockBasemapTiles,
  openProject,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

test('deck.gl map: toggles points/heatmap and restores point picking', async ({
  page,
}) => {
  await mockBasemapTiles(page);
  const pid = await createProject(page.request, uniqueName('deckgl-heatmap'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,point\n"Eiffel Tower",\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((c) => c.name === 'point')!;
  const firstRow = (
    await (
      await page.request.get(`/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=1`)
    ).json()
  ).rows[0] as { id: number };
  await setColumnType(page.request, pid, point.id, 'geo_point');
  await editCells(page.request, pid, [
    { rowId: firstRow.id, columnId: point.id, value: { lat: 48.8584, lon: 2.2945 } },
  ]);

  const deckErrors: string[] = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error' && /deck:|WebGL|could not be decoded/i.test(msg.text())) {
      deckErrors.push(msg.text());
    }
  });

  await openProject(page, pid, sheetId);
  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');

  // Switch to heatmap — the aggregation layer must render without deck/WebGL errors.
  const viewMode = page.getByTestId('map-view-mode');
  await expect(viewMode).toBeVisible();
  await viewMode.selectOption('heatmap');
  await page.waitForTimeout(600);
  expect(deckErrors, deckErrors.join('\n')).toEqual([]);

  // Switch back to points — picking a point reopens the row drawer.
  await viewMode.selectOption('points');
  await page.waitForTimeout(300);
  const canvas = page.locator('[data-testid="map-canvas-container"] canvas').first();
  await canvas.click(); // single point sits at the view center
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible({ timeout: 15_000 });
  await expect(drawer).toContainText('Eiffel Tower');
  expect(deckErrors, deckErrors.join('\n')).toEqual([]);
});
