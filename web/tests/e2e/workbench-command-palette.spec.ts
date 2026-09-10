import { expect, test } from '@playwright/test';
import { openProject, seedGeoSheet } from './helpers';

let pid: string;
let sheetId: number;

test.beforeEach(async ({ page }) => {
  ({ pid, sheetId } = await seedGeoSheet(page, {
    namePrefix: 'workbench-command-palette',
    point: { lat: 48.8584, lon: 2.2945 },
  }));
  await openProject(page, pid, sheetId);
});

test('command palette controls workbench contributions through command descriptors', async ({ page }) => {
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');

  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  await expect(palette).toHaveAttribute('data-host', 'commandPalette');

  const openSources = palette.getByTestId('workbench-command-frisket-core-command-open-sources');
  await expect(openSources).toBeVisible();
  await expect(openSources).toHaveAttribute('data-schema-version', 'frisket.command.v1');
  await expect(openSources).toHaveAttribute('data-contribution-id', 'frisket.core.command.open_sources');
  await expect(openSources).toHaveAttribute('data-host', 'commandPalette');
  await expect(openSources).toHaveAttribute('data-mode', 'command');
  await expect(openSources).toHaveAttribute('data-runtime-handler-key', 'core.commands.openContribution');
  await expect(openSources).toHaveAttribute('data-required-contributions', /frisket\.core\.panel\.sources/);

  await openSources.click();
  await expect(palette).toHaveCount(0);
  await expect(page.getByTestId('sources-connections-dialog')).toBeVisible();
  await expect(page.getByTestId('sources-panel')).toBeVisible();
  await page.getByTestId('sources-connections-close').click();

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  await expect(palette).toBeVisible();

  await palette.getByTestId('workbench-visibility-command-hide-frisket-geo-view-map').click();
  // Hidden with geo data present: the view-switcher segment goes DISABLED
  // naming the reason (never a silent omission), and the palette's reveal
  // command carries the availability state (the activity rail retired with
  // the redesign — the palette is the recovery surface).
  const mapSegment = page.getByTestId('view-switch-map');
  await expect(mapSegment).toBeDisabled();
  await expect(mapSegment).toHaveAttribute('data-disabled-reason', /reveal it via the .* palette/);
  await expect(page.getByTestId('workbench-command-palette-last-action')).toHaveText('Hid Map');

  const revealMap = palette.getByTestId('workbench-visibility-command-reveal-frisket-geo-view-map');
  await expect(revealMap).toHaveAttribute('data-command-id', 'frisket.core.command.reveal_contribution');
  await expect(revealMap).toHaveAttribute('data-target-contribution-id', 'frisket.geo.view.map');
  await expect(revealMap).toHaveAttribute('data-availability-status', 'hidden');
  await expect(revealMap).toHaveAttribute('data-availability-reason', 'hidden_by_profile');
  await revealMap.click();
  await expect(revealMap).toHaveCount(0);
  await expect(mapSegment).toBeEnabled();
  await expect(page.getByTestId('workbench-command-palette-last-action')).toHaveText('Showed Map');

  // The move-between-hosts command retired with the Window menu
  // (workbench-ia-toolbar-diet-v1): it no longer appears in the palette.
  await expect(
    palette.getByTestId('workbench-command-frisket-core-command-move-map-mainview'),
  ).toHaveCount(0);
  await expect(page.getByTestId('workbench-mainView-command-target')).toHaveCount(0);

  // The generic "Open Actions" palette command retired with the non-functional
  // custom-action authoring surface: it no longer appears in the palette.
  await expect(
    palette.getByTestId('workbench-command-frisket-core-command-run-actions'),
  ).toHaveCount(0);

  await page.getByLabel('Close command palette').click();
  await expect(palette).toBeHidden();
  await expect(page.getByTestId('workbench-region-mainView').getByTestId('grid')).toBeVisible();
});

test('ACTIONS search keeps "web search" matching the renamed Quick search action', async ({
  page,
}) => {
  // action-naming-research-cluster-v1: research.web_search's display renamed
  // 'Web search' -> 'Quick search' (web/src/actions/model.ts). The retired
  // display term stays a palette match term via ActionTemplate.keywords
  // (threaded into PaletteActionItem — WorkbenchCommandPalette.tsx's
  // matchesQuery haystack), so a user who still thinks "web search" finds it.
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();

  // Wait for the live action catalog to populate the ACTIONS section before
  // narrowing it.
  await expect(palette.getByTestId('palette-action-item').first()).toBeVisible();

  await palette.getByTestId('command-palette-input').fill('web search');
  const matchedRow = palette.locator('[data-testid="palette-action-item"][data-action-kind="research.web_search"]');
  await expect(matchedRow).toBeVisible();
  await expect(matchedRow).toContainText('Quick search');

  // An unrelated action (no 'web'/'search' in its name or keywords) stays
  // absent — confirms the match is keyword-scoped, not a blanket show-all.
  // (Not 'agent'/"Web research": "research" itself contains the substring
  // "search", so it would match this query on `name` alone regardless of
  // keywords — not a useful negative control here.)
  await expect(
    palette.locator('[data-testid="palette-action-item"][data-action-kind="enrich.geocode"]'),
  ).toHaveCount(0);
});
