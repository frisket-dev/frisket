import { expect, test } from '@playwright/test';
import { listSheets, openDiscoverTab, openProject, projectIdByName } from './helpers';

let pid: string;

test.beforeEach(async ({ page }) => {
  pid = await projectIdByName(page.request, 'Local stories');
  const sheets = await listSheets(page.request, pid);
  await openProject(page, pid, sheets[0].id);
});

test('workbench regions expose product-purpose slots instead of prototype sidebar clutter', async ({ page }) => {
  const mainView = page.getByTestId('workbench-region-mainView');
  const inspector = page.getByTestId('workbench-region-rightInspector');
  const bottomDock = page.getByTestId('workbench-region-bottomDock');
  const modalOrPeek = page.getByTestId('workbench-region-modalOrPeek');

  // The left sidebar region and the activity rail are retired
  // (workbench-ia-focus-v1 / the redesign purge).
  await expect(page.getByTestId('workbench-region-leftSidebar')).toHaveCount(0);
  await expect(mainView).toBeVisible();
  // The right inspector collapses to zero width when empty (cluster-resolve +
  // plugin panels only since the right-edge redesign) — attached, not visible.
  await expect(inspector).toBeAttached();
  await expect(bottomDock).toBeVisible();
  await expect(modalOrPeek).toBeAttached();

  // The Act region hosts the ribbon — the launcher home since the resident
  // discovery panel retired (workbench-ia-ribbon-v1 / action-drawer-v1).
  await expect(page.getByTestId('workbench-region-act')).toBeVisible();
  await expect(page.getByTestId('act-ribbon')).toBeVisible();

  // Sources re-homed into Discover; it still declares its leftSidebar placement.
  await openDiscoverTab(page, 'Sources');
  const sources = page
    .getByTestId('discover-panel')
    .getByTestId('workbench-contribution-frisket-core-panel-sources');
  await expect(sources).toBeVisible();
  await expect(sources).toHaveAttribute('data-host', 'leftSidebar');
  await expect(sources).toHaveAttribute('data-slot', 'scope');

  const grid = mainView.getByTestId('workbench-contribution-frisket-core-view-grid');
  await expect(grid).toBeVisible();
  await expect(grid).toHaveAttribute('data-host', 'mainView');
  await expect(grid).toHaveAttribute('data-slot', 'work.primary');
  await expect(mainView.getByRole('button', { name: 'Move Map' })).toHaveCount(0);
  await expect(page.getByTestId('workbench-mainView-command-target')).toHaveCount(0);
  await expect(mainView).not.toContainText('Map target: mainView');

  const jobs = bottomDock.getByTestId('workbench-contribution-frisket-core-panel-jobs');
  await expect(jobs).toBeVisible();
  await expect(jobs).toHaveAttribute('data-host', 'bottomDock');
  await expect(jobs).toHaveAttribute('data-slot', 'companion.output');
  await expect(bottomDock.getByRole('tablist', { name: 'Operational output' })).toBeVisible();

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  const openSources = palette.getByTestId('workbench-command-frisket-core-command-open-sources');
  await expect(openSources).toHaveAttribute('data-host', 'commandPalette');
  await expect(openSources).toHaveAttribute('data-slot', 'launcher');
  await expect(openSources).toHaveAttribute('data-placement-id', /frisket\.core\.command\.open_sources/);
  await page.getByLabel('Close command palette').click();

  await expect(modalOrPeek).toHaveAttribute('data-modal-stack-host', 'true');
});
