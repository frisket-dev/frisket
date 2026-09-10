// Cluster is a real catalog action. The ribbon tile opens the ACTION
// DRAWER at /action/cluster (like every other tile); the drawer body is the
// ClusterReviewForm — pick a column + method, PREVIEW groups (read-only
// /clusters/v1/preview endpoint), review the cards (editable canonicals +
// per-member exclusion checkboxes), then COMMIT cluster.values, which writes a
// {input_column}_canonical column to the source sheet. The old find → resolve →
// Entities-sheet drawer (ClusterPanel) and its right-inspector panel are GONE.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openProject,
  revealRibbonAction,
  sheetData,
  uniqueName,
} from './helpers';

// The fingerprint method keys on the NORMALIZED, punctuation-stripped, sorted
// TOKEN SET — so it collides case/spacing/word-order variants ("New York" /
// "new york" / "NEW YORK") but NOT punctuation-split variants ("N.Y.C." ->
// tokens {c,n,y}, a DIFFERENT key from "NYC" -> {nyc}). The fixture must use
// genuinely-colliding variants; the earlier N.Y.C. fixture asserted a merge
// fingerprint never performed (its failure was a fixture bug, not a product
// regression — see cluster-e2e-fixture-fix).
//
// This exact "New York" case-variant claim is no longer just hand-verified
// once and left free to drift: it's the Python-side single source of truth at
// tests/cluster_fingerprint_known_pairs.py's NEW_YORK_CASE_VARIANTS, cross-
// checked against the real fingerprint() algorithm by
// tests/engine/test_cluster_fingerprint_known_pairs.py. If this CSV's variants ever
// need to change, change them there first and confirm that check still
// passes, then mirror the change here.
const CSV =
  'city\n' +
  '"New York"\n' +
  '"new york"\n' +
  '"NEW YORK"\n' +
  '"Portland"\n' +
  '"portland"\n';

test('the Cluster tile opens the action drawer and commits a canonical column', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-cluster-drawer'));
  const sheetId = await importCsv(page.request, pid, 'cities.csv', CSV);
  await page.goto(`/p/${pid}`);

  // The tile is a real action, independent of its current ribbon category.
  await (await revealRibbonAction(page, 'cluster.values')).click();

  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/(s/\\d+/)?action/cluster$`));
  // the legacy right-inspector panel must NOT appear
  await expect(
    page.getByTestId('workbench-contribution-frisket-investigative-panel-cluster-resolve'),
  ).toHaveCount(0);
  await expect(drawer.getByTestId('cluster-review-form')).toBeVisible();

  // configure → preview → review → commit
  await drawer.getByTestId('cluster-column-select').selectOption('city');
  await expect(drawer.getByTestId('cluster-output-name')).toHaveValue('city_canonical');
  await drawer.getByTestId('cluster-preview-button').click();
  await expect(drawer.getByTestId('cluster-card').first()).toBeVisible({
    timeout: 15_000,
  });
  await drawer.getByTestId('cluster-commit-button').click();

  // the commit writes a city_canonical column whose New York-variant rows
  // collapse to one canonical and whose Portland-variant rows collapse to
  // another.
  await expect
    .poll(
      async () => {
        const data = await sheetData(page.request, pid, sheetId);
        const canonical = data.columns.find((c) => c.name === 'city_canonical');
        if (!canonical) return null;
        const values = data.rows.map((row) => row.cells[String(canonical.id)]);
        return {
          nyShared: values[0] === values[1] && values[1] === values[2],
          portlandShared: values[3] === values[4],
          distinct: values[0] !== values[3],
        };
      },
      { timeout: 30_000 },
    )
    .toEqual({ nyShared: true, portlandShared: true, distinct: true });
});

test('the legacy cluster panel command path is fully retired', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-cluster-retire'));
  const sheetId = await importCsv(page.request, pid, 'cities.csv', CSV);
  await openProject(page, pid, sheetId);

  // the command palette's Cluster entry routes to the drawer, not the panel
  await page.keyboard.press(
    process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P',
  );
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette.getByTestId('palette-action-item').first()).toBeVisible();
  await palette.getByTestId('command-palette-input').fill('cluster');
  await palette
    .locator('[data-testid="palette-action-item"][data-action-kind="cluster.values"]')
    .click();
  await expect(page.getByTestId('action-drawer')).toBeVisible();
  await expect(
    page.getByTestId('workbench-contribution-frisket-investigative-panel-cluster-resolve'),
  ).toHaveCount(0);
});
