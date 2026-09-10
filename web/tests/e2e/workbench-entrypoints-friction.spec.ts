import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openDiscoverTab,
  openPalette,
  openProject,
  projectIdByName,
  uniqueName,
} from './helpers';

// Deliberately NOT openPalette (⌘K): this exercises the app's separate
// ⌘⇧P/Ctrl+Shift+P entry point (App.tsx:1318, `key === 'p' && event.shiftKey`)
// — a real second keybinding to the same palette, not a hand-rolled duplicate
// of the ⌘K one.
async function openCommandPalette(page: Page) {
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  return palette;
}

test('hidden contributions recover through the command palette', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('entrypoints-rail'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  // Watches re-homed into the Discover panel's Watches tab
  // (workbench-ia-right-edge-v1); hide/reveal now surfaces there.
  await openDiscoverTab(page, 'Watches');
  const watchesContribution = page
    .getByTestId('discover-panel')
    .getByTestId('workbench-contribution-frisket-core-panel-watches');
  await expect(watchesContribution).toBeVisible();

  const hidePalette = await openCommandPalette(page);
  await hidePalette
    .getByTestId('workbench-visibility-command-hide-frisket-core-panel-watches')
    .click();
  await page.getByLabel('Close command palette').click();
  await expect(watchesContribution).toHaveCount(0);

  const palette = await openCommandPalette(page);
  await palette.getByTestId('workbench-visibility-command-reveal-frisket-core-panel-watches').click();
  await expect(watchesContribution).toBeVisible();
});

test('the command palette Commands section filters by the typed query, joins keyboard nav, and collapses to an empty state', async ({
  page,
}) => {
  // The Commands section (first-party commands, plugin launchers, hide/
  // reveal toggles) is production work like BEST MATCH/ACTIONS/GO TO and must
  // filter live too, with the same keyboard-nav/empty-state/placeholder
  // coupling those sections already have.
  const pid = await createProject(page.request, uniqueName('entrypoints-palette-filter'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  const palette = await openCommandPalette(page);
  const queryInput = page.getByTestId('command-palette-input');
  const commandsSection = page.getByTestId('palette-section-commands');
  const commandRows = commandsSection.locator('.workbench-command-palette-item');
  const openSources = palette.getByTestId('workbench-command-frisket-core-command-open-sources');

  // Placeholder text now names commands as a filterable target too.
  await expect(queryInput).toHaveAttribute('placeholder', /commands/i);

  // Unfiltered: several first-party commands render, "Open Sources" among them.
  await expect(openSources).toBeVisible();
  const unfilteredCount = await commandRows.count();
  expect(unfilteredCount).toBeGreaterThan(1);

  // Narrowing to "sources" shrinks the Commands section but keeps the match.
  await queryInput.fill('sources');
  await expect(openSources).toBeVisible();
  const filteredCount = await commandRows.count();
  expect(filteredCount).toBeGreaterThan(0);
  expect(filteredCount).toBeLessThan(unfilteredCount);

  // A query with no match anywhere collapses every section — including
  // Commands, which no longer dangles an empty "Commands" header — and shows
  // the overall empty state instead.
  await queryInput.fill('zzz-no-such-command-or-action-zzz');
  await expect(page.getByTestId('palette-section-commands')).toHaveCount(0);
  await expect(page.getByTestId('palette-section-best-match')).toHaveCount(0);
  await expect(page.getByTestId('palette-section-actions')).toHaveCount(0);
  await expect(page.getByTestId('palette-section-goto')).toHaveCount(0);
  await expect(page.getByTestId('palette-empty-state')).toBeVisible();

  // A query matched by exactly TWO commands and nothing else ("Open Sources"
  // + "Open Settings" both title-match "open") proves ArrowDown actually
  // traverses the filtered Commands rows, not just BEST MATCH/ACTIONS/GO TO —
  // the first filtered row starts active (same default-to-index-0 the
  // sibling sections already have), and arrowing down moves off it.
  await queryInput.fill('open');
  await expect(page.getByTestId('palette-section-best-match')).toHaveCount(0);
  await expect(page.getByTestId('palette-section-actions')).toHaveCount(0);
  await expect(page.getByTestId('palette-section-goto')).toHaveCount(0);
  await expect(commandRows).toHaveCount(2);
  await expect(commandRows.nth(0)).toHaveAttribute('data-active', 'true');
  await expect(commandRows.nth(1)).toHaveAttribute('data-active', 'false');
  await queryInput.press('ArrowDown');
  await expect(commandRows.nth(0)).toHaveAttribute('data-active', 'false');
  await expect(commandRows.nth(1)).toHaveAttribute('data-active', 'true');

  // A query matched by exactly ONE command activates it by default (no arrow
  // press needed, same as a single BEST MATCH row would) — Enter runs it.
  await queryInput.fill('open sources');
  await expect(commandRows).toHaveCount(1);
  await expect(openSources).toHaveAttribute('data-active', 'true');
  await queryInput.press('Enter');
  await expect(page.getByTestId('workbench-contribution-frisket-core-panel-sources')).toBeVisible();
});

test('Search this project closes on Escape from controls and result focus', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  await openPalette(page);
  await page.getByTestId('search-mode-select').focus();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('workbench-region-commandPalette')).toHaveCount(0);

  await openPalette(page);
  await page.getByTestId('command-palette-input').fill('listeria');
  await expect(page.getByTestId('search-hit').first()).toBeVisible();
  await page.getByTestId('search-hit').first().focus();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('workbench-region-commandPalette')).toHaveCount(0);
});

test('Sources panel does not render an independent add-source form', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('entrypoints-sources'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  await openDiscoverTab(page, 'Sources');
  const sources = page.getByTestId('workbench-contribution-frisket-core-panel-sources');
  await expect(sources.getByTestId('sources-panel')).toBeVisible();
  await expect(sources.getByTestId('source-add-button')).toHaveCount(0);
  await expect(sources.getByTestId('source-form')).toHaveCount(0);
  // Import is reachable via the ribbon Import tab (the sidebar quick-action
  // retired in workbench-ia-focus-v1), not an in-panel add-source form.
  await page.getByTestId('ribbon-tab-data').click();
  await expect(page.getByTestId('ribbon-command-import')).toBeVisible();
});

test('notification feed has no inline settings link; channel/route config lives in project settings', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('entrypoints-notifications'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  await openDiscoverTab(page, 'Notifications');
  await expect(page.getByTestId('notifications-panel')).toBeVisible();
  await expect(page.getByTestId('notification-settings-panel')).toHaveCount(0);
  // The out-of-style inline "Notification settings" link was removed; the feed
  // no longer offers it. Channel/route config lives in project settings.
  await expect(page.getByTestId('notifications-settings-link')).toHaveCount(0);

  await page.goto(`/p/${pid}/settings/project/notifications`);
  await expect(page.getByTestId('settings-section-project-notifications')).toBeVisible();
  await expect(page.getByTestId('notification-settings-panel')).toBeVisible();
});
