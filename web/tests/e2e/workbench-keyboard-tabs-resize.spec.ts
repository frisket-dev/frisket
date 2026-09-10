import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';

function numericStylePx(value: string | null): number {
  return Number(String(value ?? '').replace('px', ''));
}

test('workbench tablists support keyboard selection and resize handles persist', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('keyboard-tabs-resize'));
  const firstSheet = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  const secondSheet = await importCsv(page.request, pid, 'beta.csv', 'name\nBeta\n');
  await openProject(page, pid, firstSheet);

  const firstTab = page.getByTestId(`workbench-mainView-tab-${firstSheet}`);
  const secondTab = page.getByTestId(`workbench-mainView-tab-${secondSheet}`);
  await expect(firstTab).toHaveAttribute('role', 'tab');
  await expect(firstTab).toHaveAttribute('aria-selected', 'true');
  await firstTab.focus();
  await page.keyboard.press('ArrowRight');
  await expect(secondTab).toBeFocused();
  await expect(secondTab).toHaveAttribute('aria-selected', 'true');
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${secondSheet}$`));
  await page.keyboard.press('Home');
  await expect(firstTab).toBeFocused();
  await expect(firstTab).toHaveAttribute('aria-selected', 'true');

  const bottomTablist = page.getByTestId('bottom-dock-tablist');
  await expect(bottomTablist).toHaveAttribute('role', 'tablist');
  const jobsTab = page.getByTestId('bottom-dock-tab-jobs');
  const errorsTab = page.getByTestId('bottom-dock-tab-errors');
  await expect(jobsTab).toHaveAttribute('aria-selected', 'true');
  await jobsTab.focus();
  await page.keyboard.press('ArrowRight');
  await expect(errorsTab).toBeFocused();
  await expect(errorsTab).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'aria-labelledby',
    'workbench-bottom-dock-tab-errors',
  );
  // Dock tab identity is the resolved placementId;
  // the last dock placement (Lineage) keeps its pinned 'lineage' placementId —
  // the Plugins tab retired from the dock entirely.
  await page.keyboard.press('End');
  await expect(page.getByTestId('bottom-dock-tab-lineage')).toBeFocused();
  await expect(page.getByTestId('bottom-dock-tab-lineage')).toHaveAttribute(
    'aria-selected',
    'true',
  );

  const bottomDock = page.getByTestId('workbench-region-bottomDock');
  const bottomResize = page.getByTestId('bottom-dock-resize');
  const initialHeight = numericStylePx(await bottomDock.evaluate((node) => getComputedStyle(node).height));
  await bottomResize.focus();
  await page.keyboard.press('ArrowUp');
  const storedHeight = await page.evaluate(() => localStorage.getItem('frisket:bottom-dock-height'));
  expect(Number(storedHeight)).toBeGreaterThan(initialHeight);
  await expect(bottomDock).toHaveCSS('height', `${storedHeight}px`);

  // The action surface is now a fixed-400px overlay drawer with no resize handle
  // (workbench-ia-action-drawer-v1; src/components/ActionPanel.tsx drawer variant).
  // The formerly-resident resizable action panel (action-panel-resize +
  // frisket:action-panel-width persistence) retired with it, so there is no
  // action-panel width-resize surface left to assert.
});
