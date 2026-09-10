import { expect, test } from '@playwright/test';
import {
  clickHeaderMenu,
  mockBasemapTiles,
  openProject,
  seedGeoSheet,
  sheetColumns,
} from './helpers';

// projection-status-chip-v1: the active map projection's status used to own a
// whole bottom-dock tab for what is always a single row. It now lives as a
// status-bar chip beside the run/idle indicator, expanding to a popover with
// the full backend/schema/generation detail on click.
test('active map projection status is exposed through a status-bar chip', async ({
  page,
}) => {
  const { pid, sheetId, pointId } = await seedGeoSheet(page, {
    namePrefix: 'workbench-projection-status',
    point: { lat: 48.8584, lon: 2.2945 },
  });
  const point = { id: Number(pointId) };
  const columns = await sheetColumns(page.request, pid, sheetId);
  await mockBasemapTiles(page);

  await openProject(page, pid, sheetId);
  await clickHeaderMenu(page, columns, 'point');
  const openMap = page.getByTestId('header-menu-open-map');
  await expect(openMap).toBeVisible();
  await openMap.click();

  await expect(page.getByTestId('workbench-mainView-split')).toBeVisible();
  await expect(page.getByTestId('workbench-contribution-frisket-geo-view-map')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');

  // The chip appears in the status bar once a projection is active and carries
  // the same machine-readable projection metadata the dock panel used to expose.
  const chip = page.getByTestId('projection-chip');
  await expect(chip).toBeVisible();
  await expect(chip).toHaveAttribute('data-active-contribution-id', 'frisket.geo.view.map');
  await expect(chip).toHaveAttribute('data-projection-sheet-id', String(sheetId));
  await expect(chip).toHaveAttribute('data-projection-column-id', String(point.id));
  await expect(chip).toHaveAttribute('data-projection-backend', 'sqlite-rtree');
  await expect(chip).toHaveAttribute('data-projection-schema', 'frisket.map_points.arrow.v1');
  await expect(chip).toHaveAttribute('data-projection-transient', 'false');
  await expect(chip).toHaveAttribute('data-projection-valid-points', '1');
  await expect(chip).toHaveAttribute('data-projection-generation', /.+/);
  await expect(chip).toContainText('1 pt');

  // Clicking expands the detail popover.
  await chip.click();
  const watcher = page.getByTestId('projection-watcher');
  await expect(watcher).toBeVisible();
  await expect(watcher.getByTestId('projection-status-valid-points')).toHaveText('1 valid point');
  await expect(watcher.getByTestId('projection-status-freshness')).toHaveText('Ready');
  await expect(watcher).toContainText('sqlite-rtree');
  await expect(watcher).toContainText('point');
});
