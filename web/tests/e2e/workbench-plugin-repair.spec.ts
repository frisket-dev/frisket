import { expect, test } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  mockBasemapTiles,
  openProject,
  setColumnType,
  setHiddenContributions,
  sheetColumns,
  uniqueName,
} from './helpers';

test('Map layout recovery is hide and reveal through the command palette', async ({
  page,
}) => {
  await mockBasemapTiles(page);
  const pid = await createProject(page.request, uniqueName('map-layout-recovery'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,point\n"Eiffel Tower",\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((c) => c.name === 'point')!;
  const data = await page.request.get(
    `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=1`,
  );
  const firstRow = (await data.json()).rows[0] as { id: number };
  await setColumnType(page.request, pid, point.id, 'geo_point');
  await editCells(page.request, pid, [
    { rowId: firstRow.id, columnId: point.id, value: { lat: 48.8584, lon: 2.2945 } },
  ]);
  await setHiddenContributions(page, pid, ['frisket.geo.view.map']);
  await openProject(page, pid, sheetId);

  // Hide/reveal round-trips through the ⌘K palette (the activity rail's
  // recovery entries retired with the redesign); a hidden map never grows a
  // repair affordance.
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  const revealMap = palette.getByTestId('workbench-visibility-command-reveal-frisket-geo-view-map');
  await expect(revealMap).toHaveAttribute('data-availability-status', 'hidden');
  await expect(revealMap).toHaveAttribute('data-availability-reason', 'hidden_by_profile');
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);

  await revealMap.click();
  await expect(revealMap).toHaveCount(0);
  await page.getByLabel('Close command palette').click();
  // The map split is workspace chrome state (splitfix), not a route: the URL
  // stays on the sheet while the split opens.
  await page.getByTestId('view-switch-map').click();
  await expect(page.getByTestId('workbench-contribution-frisket-geo-view-map')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${sheetId}$`));

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  await expect(palette).toBeVisible();
  await palette.getByTestId('workbench-visibility-command-hide-frisket-geo-view-map').click();
  // Hiding is still recovery-only: the reveal command returns and no repair
  // affordance appears.
  await expect(
    palette.getByTestId('workbench-visibility-command-reveal-frisket-geo-view-map'),
  ).toHaveAttribute('data-availability-status', 'hidden');
  await page.getByLabel('Close command palette').click();
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});
