import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';

async function openCommandPalette(page: Page) {
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  return palette;
}

// Cohort 1 (plugin-bottomdock-tab-parity-v1): the bottom dock renders resolved
// bottomDock:tab contributions — no hardcoded tab list decides what renders.

// Reflects the full resolved bottomDock set: Lineage joined at inc7 (Monitor);
// the OCR compare tab retired when the bake-off moved to its scratch center
// tab (workbench-ocr-compare-bakeoff-v1). The projection_status tab retired to
// a status-bar chip (projection-status-chip-v1) — it was a guaranteed one-row
// readout, over-weight as a dock tab; see workbench-projection-status.spec.ts.
// The preview tab retired as a dead signpost (its content never rendered real
// preview output; previews open as sheet tabs above the grid). The plugins
// tab retired —
// it was a read-only duplicate of the full manager Settings already hosts;
// see workbench-plugin-runtime-index.spec.ts for the Settings-route coverage
// and workbench-plugin-failure-errors.spec.ts for the Errors-routing coverage.
const EXPECTED_TAB_ORDER = [
  { placementId: 'jobs', contributionId: 'frisket.core.panel.jobs', label: 'Jobs' },
  { placementId: 'errors', contributionId: 'frisket.core.panel.errors', label: 'Errors' },
  { placementId: 'history', contributionId: 'frisket.core.panel.history', label: 'History' },
  { placementId: 'lineage', contributionId: 'frisket.core.panel.lineage', label: 'Lineage' },
];

test('bottom dock renders resolved contributions in placement order', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('bottom-dock-contributions'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  const tablist = page.getByTestId('bottom-dock-tablist');
  const tabs = tablist.locator('[data-testid^="bottom-dock-tab-"]:not([data-testid^="bottom-dock-tab-close-"])');
  await expect(tabs).toHaveCount(EXPECTED_TAB_ORDER.length);
  for (const [index, expected] of EXPECTED_TAB_ORDER.entries()) {
    const tab = tabs.nth(index);
    await expect(tab).toHaveAttribute('data-placement-id', expected.placementId);
    await expect(tab).toHaveAttribute('data-contribution-id', expected.contributionId);
    await expect(tab).toHaveAttribute('data-runtime-source', 'firstParty');
    await expect(tab).toHaveText(expected.label);
  }
});

test('the palette hides dock tabs and the active tab falls back to jobs', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('bottom-dock-visibility'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  // Make History the active tab, then hide it via the ⌘K palette.
  await page.getByTestId('bottom-dock-tab-history').click();
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-placement-id',
    'history',
  );

  const palette = await openCommandPalette(page);
  await palette.getByTestId('workbench-visibility-command-hide-frisket-core-panel-history').click();
  await page.getByLabel('Close command palette').click();

  // The hidden tab is gone and the dock fell back to the jobs anchor.
  await expect(page.getByTestId('bottom-dock-tab-history')).toHaveCount(0);
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-placement-id',
    'jobs',
  );

  // Reveal restores the tab through the palette.
  const revealPalette = await openCommandPalette(page);
  await revealPalette
    .getByTestId('workbench-visibility-command-reveal-frisket-core-panel-history')
    .click();
  await page.getByLabel('Close command palette').click();
  await expect(page.getByTestId('bottom-dock-tab-history')).toBeVisible();
});

test('dock tablist keyboard navigation walks the resolved tab list', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('bottom-dock-keyboard'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  const jobsTab = page.getByTestId('bottom-dock-tab-jobs');
  await jobsTab.focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByTestId('bottom-dock-tab-errors')).toBeFocused();
  await page.keyboard.press('End');
  await expect(page.getByTestId('bottom-dock-tab-lineage')).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(jobsTab).toBeFocused();
  await expect(jobsTab).toHaveAttribute('aria-selected', 'true');
});
