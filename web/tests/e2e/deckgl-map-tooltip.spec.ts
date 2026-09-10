// Hover tooltip on the deck.gl map: hovering a point shows its coordinates, and
// (when a Color-by/Size-by column is active) that column's name + value.

import { expect, test } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  mockBasemapTiles,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

test('deck.gl map: hovering a point shows a tooltip with coords and attribute value', async ({
  page,
}) => {
  await mockBasemapTiles(page);
  const pid = await createProject(page.request, uniqueName('deckgl-tooltip'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,category,point\n"Eiffel Tower","Landmark",\n',
  );
  let columns = await sheetColumns(page.request, pid, sheetId);
  const col = (name: string) => columns.find((c) => c.name === name)!;
  const data = await page.request.get(
    `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=1`,
  );
  const firstRow = (await data.json()).rows[0] as { id: number };
  await setColumnType(page.request, pid, col('point').id, 'geo_point');
  await editCells(page.request, pid, [
    { rowId: firstRow.id, columnId: col('point').id, value: { lat: 48.8584, lon: 2.2945 } },
  ]);
  columns = await sheetColumns(page.request, pid, sheetId);

  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${col('point').id}`);
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');

  const canvas = page.locator('[data-testid="map-canvas-container"] canvas').first();
  const box = await canvas.boundingBox();
  if (!box) throw new Error('no canvas');
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;

  // The single point sits at the view center; hovering it shows the tooltip.
  const tooltip = page.getByTestId('map-tooltip');
  await expect
    .poll(
      async () => {
        await page.mouse.move(cx, cy);
        return tooltip.isVisible();
      },
      { timeout: 8000 },
    )
    .toBe(true);
  await expect(tooltip).toContainText('48.85840, 2.29450');

  // With a color-by column active, the tooltip names the column and its value.
  await page.getByTestId('map-color-by').selectOption(String(col('category').id));
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');
  await page.mouse.move(cx + 40, cy + 40); // off the point to reset hover
  await expect
    .poll(
      async () => {
        await page.mouse.move(cx, cy);
        return (await tooltip.isVisible()) && (await tooltip.textContent());
      },
      { timeout: 8000 },
    )
    .toContain('Landmark');
});
