import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';

// FollowTheMoney's export_target ui_hint moved off the
// project.export surface, so exportTargetsFromCatalog drops it from the
// project export-target flyout. The shared Act model exposes Export as one
// category at both ribbon and compact densities, with the two direct export
// commands. The original check also pinned FtM as reachable via two static
// launcher placements; that clause is SUPERSEDED
// by the frisket.ftm plugin migration (91fa5dbe/182f7499):
// the core ftm_export/ftm_import kinds are deleted and FtM ships as the
// dormant bundled plugin, so this spec now pins full ABSENCE from the
// static launcher surfaces too.

test('FtM is absent from the project flyout and shared Export category; every direct export dispatches', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('export-target-reconcile'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'reconcile.csv',
    'name,note\nAda,hello\nGrace,world\n',
  );
  await openProject(page, pid, sheetId);

  // --- Project ▾ Export-data flyout (TopNav.tsx ExportDataGroup) ---
  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();
  await page.getByTestId('export-menu-open').click();
  await expect(page.getByTestId('export-menu')).toBeVisible();
  // Wait for the async catalog fetch to resolve past the "Loading…" state
  // before reading the rendered row set.
  await expect(page.getByTestId('export-target-csv')).toBeVisible();
  await expect(page.getByTestId('export-target-followthemoney')).toHaveCount(0);
  const projectMenuRowIds = (
    await page.getByTestId('export-menu').locator('[data-testid^="export-target-"]').all()
  ).map((row) => row.getAttribute('data-testid'));
  expect((await Promise.all(projectMenuRowIds)).sort()).toEqual([
    'export-target-csv',
    'export-target-google_sheets',
  ]);

  // Dispatchability: the csv row opens the CSV modal (no dead rows).
  await page.getByTestId('export-target-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await page.getByTestId('export-csv-cancel').click();
  await expect(page.getByTestId('export-data-modal')).toHaveCount(0);

  // Dispatchability: the google_sheets row opens the Google Sheets modal.
  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();
  await page.getByTestId('export-menu-open').click();
  await page.getByTestId('export-target-google_sheets').click();
  await expect(page.getByTestId('export-google-sheets-modal')).toBeVisible();
  await page.getByTestId('export-google-sheets-cancel').click();
  await expect(page.getByTestId('export-google-sheets-modal')).toHaveCount(0);

  // --- Shared Data category: ribbon density ---
  await page.getByTestId('ribbon-tab-data').click();
  await expect(page.getByTestId('ribbon-command-export-csv')).toBeVisible();
  await expect(page.getByTestId('ribbon-command-export-google-sheets')).toBeVisible();
  await expect(page.getByTestId('ribbon-action-ftm_export')).toHaveCount(0);

  await page.getByTestId('ribbon-command-export-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await page.getByTestId('export-csv-cancel').click();
  await expect(page.getByTestId('export-data-modal')).toHaveCount(0);

  await page.getByTestId('ribbon-command-export-google-sheets').click();
  await expect(page.getByTestId('export-google-sheets-modal')).toBeVisible();
  await page.getByTestId('export-google-sheets-cancel').click();
  await expect(page.getByTestId('export-google-sheets-modal')).toHaveCount(0);

  // --- Shared Data category: compact density ---
  // The compact menu is a direct rendering of the same resolved category,
  // not a separate Data ▾ Export… flyout.
  await page.getByTestId('ribbon-collapse').click();
  await page.getByTestId('menubar-menu-data').click();
  await expect(page.getByTestId('menubar-dropdown-data')).toBeVisible();
  await expect(page.getByTestId('menu-command-export-csv')).toBeVisible();
  await expect(page.getByTestId('menu-command-export-google-sheets')).toBeVisible();
  await expect(page.getByTestId('menu-action-ftm_export')).toHaveCount(0);

  await page.getByTestId('menu-command-export-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await page.getByTestId('export-csv-cancel').click();
  await expect(page.getByTestId('export-data-modal')).toHaveCount(0);

  await page.getByTestId('menubar-menu-data').click();
  await page.getByTestId('menu-command-export-google-sheets').click();
  await expect(page.getByTestId('export-google-sheets-modal')).toBeVisible();
  await page.getByTestId('export-google-sheets-cancel').click();
  await expect(page.getByTestId('export-google-sheets-modal')).toHaveCount(0);
});
