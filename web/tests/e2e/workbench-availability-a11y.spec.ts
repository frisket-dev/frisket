import { expect, test, type Page } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  importCsv,
  openProject,
  seedGeoSheet,
  setHiddenContributions,
  sheetColumns,
  uniqueName,
} from './helpers';

async function createGeoProject(page: Page) {
  const { pid, sheetId, pointId } = await seedGeoSheet(page, {
    namePrefix: 'availability-a11y',
    point: { lat: 48.8584, lon: 2.2945 },
  });
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((c) => c.id === Number(pointId))!;
  return { pid, sheetId, columns, point };
}

async function openCommandPalette(page: Page) {
  const palette = page.getByTestId('workbench-region-commandPalette');
  // Re-press until the palette mounts: right after navigation the shortcut can
  // land before the workbench keybinding handler attaches.
  await expect(async () => {
    await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
    await expect(palette).toBeVisible({ timeout: 1_500 });
  }).toPass({ timeout: 10_000 });
  return palette;
}

// Seed the standalone visibility store with nothing hidden → Map stays enabled.
function installEnabledMapPreset(projectId: string) {
  localStorage.setItem(
    `frisket:contribution-visibility:${projectId}`,
    JSON.stringify({
      schemaVersion: 'frisket.contribution_visibility.v1',
      hiddenContributionIds: [],
    }),
  );
}

test('hidden Map is unavailable across launchers until the layout is revealed', async ({ page }) => {
  const { pid, sheetId, columns, point } = await createGeoProject(page);
  await setHiddenContributions(page, pid, ['frisket.geo.view.map']);
  await openProject(page, pid, sheetId);

  // Map hidden with geo data present → the view-switcher segment renders
  // DISABLED naming the reason (never a silent omission).
  const mapSegment = page.getByTestId('view-switch-map');
  await expect(mapSegment).toBeDisabled();
  await expect(mapSegment).toHaveAttribute('data-disabled-reason', /reveal it via the .* palette/);

  await clickHeaderMenu(page, columns, 'point');
  const headerMap = page.getByTestId('header-menu-open-map');
  await expect(headerMap).toBeVisible();
  await expect(headerMap).toBeDisabled();
  await expect(headerMap).toHaveAttribute('data-availability-reason', 'hidden_by_profile');
  await page.keyboard.press('Escape');

  // The ⌘K palette is the recovery surface (the activity rail retired): its
  // reveal command carries the hidden state and the real reason.
  const hiddenPalette = await openCommandPalette(page);
  const revealMap = hiddenPalette.getByTestId(
    'workbench-visibility-command-reveal-frisket-geo-view-map',
  );
  await expect(revealMap).toHaveAttribute('data-availability-status', 'hidden');
  await expect(revealMap).toHaveAttribute('data-availability-reason', 'hidden_by_profile');
  await page.getByLabel('Close command palette').click();

  // A deep link cannot smuggle a hidden map in.
  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  await expect(page.getByTestId('workbench-mainView-split')).toHaveCount(0);
  await expect(page.getByTestId('workbench-contribution-frisket-geo-view-map')).toHaveCount(0);
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);

  // Revealing through the palette restores the map without a page reload.
  const revealPalette = await openCommandPalette(page);
  await revealPalette
    .getByTestId('workbench-visibility-command-reveal-frisket-geo-view-map')
    .click();
  await page.getByLabel('Close command palette').click();
  await expect(page.getByTestId('view-switch-map')).toBeEnabled();
  await page.getByTestId('view-switch-map').click();
  await expect(page.getByTestId('workbench-contribution-frisket-geo-view-map')).toBeVisible();
});

test('enabled Map remains disabled when no geo_point column satisfies data requirements', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('availability-no-geo'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'place,notes\n"Eiffel Tower","text only"\n',
  );
  await page.addInitScript(installEnabledMapPreset, pid);
  await openProject(page, pid, sheetId);

  // No geo_point column satisfies data requirements → the map view-switcher
  // segment is data-omitted entirely (segments are data-keyed).
  await expect(page.getByTestId('view-switch-map')).toHaveCount(0);

  // The palette's map command advertises the disabled state and the real
  // reason (the activity rail's recovery entry retired).
  const palette = await openCommandPalette(page);
  const paletteMap = palette.getByTestId('workbench-visibility-command-hide-frisket-geo-view-map');
  await expect(paletteMap).toHaveAttribute('data-availability-status', 'disabled');
  await expect(paletteMap).toHaveAttribute('data-availability-reason', 'data_requirements_unmet');
  await page.getByLabel('Close command palette').click();
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${sheetId}$`));
});
