import { expect, test } from '@playwright/test';
import {
  listSheets,
  openAction,
  openDiscoverTab,
  openProject,
  projectIdByName,
} from './helpers';

let pid: string;

test.beforeEach(async ({ page }) => {
  pid = await projectIdByName(page.request, 'Local stories');
  const sheets = await listSheets(page.request, pid);
  await openProject(page, pid, sheets[0].id);
});

test('Sources, History, and Actions expose honest portable placements', async ({ page }) => {
  const leftSidebar = page.getByTestId('workbench-region-leftSidebar');
  const rightInspector = page.getByTestId('workbench-region-rightInspector');
  const rightInspectorLayout = page.getByTestId('workbench-resolved-layout-region-rightInspector');
  const bottomDock = page.getByTestId('workbench-region-bottomDock');
  const bottomLayout = page.getByTestId('workbench-resolved-layout-region-bottomDock');

  // Sources re-homed into the Discover panel (workbench-ia-right-edge-v1); it
  // still declares its leftSidebar placement (data-host), it just renders there.
  await openDiscoverTab(page, 'Sources');
  const sources = page
    .getByTestId('discover-panel')
    .getByTestId('workbench-contribution-frisket-core-panel-sources');
  await expect(sources).toBeVisible();
  await expect(sources).toHaveAttribute('data-host', 'leftSidebar');
  await expect(sources).toHaveAttribute('data-slot', 'scope');
  await expect(bottomLayout).not.toHaveAttribute('data-contribution-ids', /frisket\.core\.panel\.sources/);
  // The interim sidebar no longer renders Sources.
  await expect(
    leftSidebar.getByTestId('workbench-contribution-frisket-core-panel-sources'),
  ).toHaveCount(0);

  // History is bottom-dock-only now — it must not render in the sidebar.
  await expect(
    leftSidebar.getByTestId('workbench-contribution-frisket-core-panel-history'),
  ).toHaveCount(0);

  await bottomDock.getByRole('tab', { name: 'History' }).click();
  const dockHistory = bottomDock.getByTestId('workbench-contribution-frisket-core-panel-history');
  await expect(dockHistory).toBeVisible();
  await expect(dockHistory).toHaveAttribute('data-host', 'bottomDock');
  await expect(dockHistory).toHaveAttribute('data-slot', 'companion.output');
  await expect(dockHistory).toHaveAttribute('data-runtime-component-key', 'core.panels.HistoryPanel');

  // Actions are EXPRESSED by the shell, never PLACED: there is no
  // frisket.core.panel.actions descriptor at all, so
  // nothing resident renders in the rightInspector and the resolved layout for
  // that region never lists it. The action's only home is the overlay
  // ActionDrawer — an expression of the action, not a slotted panel.
  await expect(
    rightInspector.getByTestId('workbench-contribution-frisket-core-panel-actions'),
  ).toHaveCount(0);
  await expect(
    rightInspector.getByTestId('action-panel'),
  ).toHaveCount(0);
  await expect(rightInspectorLayout).not.toHaveAttribute(
    'data-contribution-ids',
    /frisket\.core\.panel\.actions/,
  );
  await expect(bottomLayout).not.toHaveAttribute('data-contribution-ids', /frisket\.core\.panel\.actions/);

  // The real expression: launching an action opens the overlay drawer with the
  // action FORM (App.tsx WorkspaceActionDrawerRegion -> ActionDrawer), the only
  // home the action has.
  await openAction(page, 'map.summarize');
  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId('generated-action-form')).toBeVisible();
  await expect(drawer.getByTestId('action-form-title')).toContainText(/summar/i);
  await drawer.getByTestId('action-drawer-close').click();
  await expect(drawer).toHaveCount(0);

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  const openSources = palette.getByTestId('workbench-command-frisket-core-command-open-sources');
  await expect(openSources).toHaveAttribute('data-required-contributions', /frisket\.core\.panel\.sources/);
});
