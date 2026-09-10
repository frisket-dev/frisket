import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openDiscoverTab,
  openProject,
  revealRibbonAction,
  uniqueName,
} from './helpers';

async function openCommandPalette(page: Page) {
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  return palette;
}

// First-party data panels render through the resolved Discover contributions;
// no hardcoded panel arrangement or portable-slot props decide what renders
// where. Search lives in the ⌘K palette, Copilot in the ✧ popover, and the data
// panels in Discover.

test('the left sidebar is retired; re-homed panels live in Discover and chrome stays intact', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('sidebar-contributions'));
  const firstSheet = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  const secondSheet = await importCsv(page.request, pid, 'beta.csv', 'name\nBeta\n');
  await openProject(page, pid, firstSheet);

  // No sidebar shell, and no resident Search/Copilot contribution wrappers.
  await expect(page.locator('.sidebar')).toHaveCount(0);
  await expect(page.getByTestId('workbench-region-leftSidebar')).toHaveCount(0);
  await expect(page.locator('[data-testid^="sidebar-contribution-"]')).toHaveCount(0);

  // The re-homed data panels reach the Discover panel (declared-order source of
  // truth still the resolved layout).
  await openDiscoverTab(page, 'Sources');
  await expect(
    page.getByTestId('discover-panel').getByTestId('workbench-contribution-frisket-core-panel-sources'),
  ).toBeVisible();

  // Chrome regression guard: sheet navigation via the tab strip, the project
  // switcher in the chrome bar, and an action reachable through the ribbon.
  await page.getByTestId(`workbench-mainView-tab-${secondSheet}`).click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${secondSheet}$`));
  await page.getByTestId(`workbench-mainView-tab-${firstSheet}`).click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${firstSheet}$`));
  await expect(page.getByTestId('chrome-bar').getByTestId('switch-project')).toBeVisible();
  await revealRibbonAction(page, 'media.fetch_url');
});

test('the palette hides a re-homed panel (Watches) and it recovers', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('sidebar-visibility'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  // Watches now renders in the Discover panel's Watches tab.
  await openDiscoverTab(page, 'Watches');
  const discover = page.getByTestId('discover-panel');
  await expect(
    discover.getByTestId('workbench-contribution-frisket-core-panel-watches'),
  ).toBeVisible();

  const hidePalette = await openCommandPalette(page);
  await hidePalette
    .getByTestId('workbench-visibility-command-hide-frisket-core-panel-watches')
    .click();
  await page.getByLabel('Close command palette').click();

  // Contribution-scoped hide: the Discover panel body drops it AND the dock tab drops.
  await expect(
    discover.getByTestId('workbench-contribution-frisket-core-panel-watches'),
  ).toHaveCount(0);
  await expect(page.getByTestId('bottom-dock-tab-watches')).toHaveCount(0);

  // Revealing through the palette restores it to the Discover panel and the dock.
  const revealPalette = await openCommandPalette(page);
  await revealPalette
    .getByTestId('workbench-visibility-command-reveal-frisket-core-panel-watches')
    .click();
  await page.getByLabel('Close command palette').click();
  await expect(
    discover.getByTestId('workbench-contribution-frisket-core-panel-watches'),
  ).toBeVisible();
  await expect(page.getByTestId('bottom-dock-tab-watches')).toBeVisible();
});

test('embeddings panel remounts per sheet with activeSheet gating preserved', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('sidebar-embeddings'));
  const firstSheet = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  const secondSheet = await importCsv(page.request, pid, 'beta.csv', 'name\nBeta\n');
  await openProject(page, pid, firstSheet);

  // Embeddings now renders in the Discover panel's Embeddings tab, always
  // open (no second collapse level).
  await openDiscoverTab(page, 'Embeddings');
  const embeddingsWrapper = page.getByTestId(
    'workbench-contribution-frisket-embeddings-panel-indexes',
  );
  await expect(embeddingsWrapper).toBeAttached();
  await expect(embeddingsWrapper.getByTestId('embeddings-panel')).toBeVisible();

  // Open the create form: this is component state that ONLY a remount
  // resets — switching sheets must reset it (the per-sheet key), proving that
  // always-open panels retain per-sheet remount semantics.
  await embeddingsWrapper.getByTestId('embeddings-add-button').click();
  await expect(embeddingsWrapper.getByTestId('embeddings-form')).toBeVisible();

  // The create dialog is a real full-viewport modal (no backdrop-click-to-
  // close), so it visually covers the sheet tab strip too; dispatch the
  // click event directly on the tab to exercise the SPA sheet switch rather
  // than asserting on a UX question (whether the modal should block
  // sheet-switching) that is out of scope here.
  await page.getByTestId(`workbench-mainView-tab-${secondSheet}`).dispatchEvent('click');
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${secondSheet}$`));
  await expect(embeddingsWrapper.getByTestId('embeddings-panel')).toBeVisible();
  await expect(embeddingsWrapper.getByTestId('embeddings-form')).toHaveCount(0);
});
