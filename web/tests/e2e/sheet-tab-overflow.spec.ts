import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';

test('extra sheets are available through a menu without a tab scrollbar', async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 900 });
  const pid = await createProject(page.request, uniqueName('sheet-overflow'));
  const sheets: { id: string; name: string }[] = [];
  for (let index = 1; index <= 14; index += 1) {
    const name = index === 14 ? 'Council report with a much longer descriptive title' : `Council report ${String(index).padStart(2, '0')}`;
    sheets.push({ name, id: await importCsv(page.request, pid, `${name}.csv`, 'note\nA council decision.\n') });
  }
  await openProject(page, pid, sheets[0].id);
  const strip = page.getByTestId('workbench-mainView-tabs');
  const more = strip.getByRole('button', { name: 'More tabs', exact: true });
  await expect(more).toBeVisible();
  await expect(strip.locator('[role="tablist"]')).not.toHaveCSS('overflow-x', 'auto');
  await expect(strip.locator('[role="tablist"]')).not.toHaveCSS('overflow-x', 'scroll');
  await more.click();
  const menu = page.getByRole('menu');
  const last = sheets.at(-1)!;
  await expect(menu.getByText(last.name, { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('sheet-overflow-menu.png'), fullPage: true });
  await menu.getByText(last.name, { exact: true }).click();
  const selected = page.getByTestId(`workbench-mainView-tab-${last.id}`);
  await expect(selected).toHaveAttribute('aria-selected', 'true');
  await expect(selected).toBeInViewport();
  const selectedBounds = await selected.boundingBox();
  const moreBounds = await more.boundingBox();
  expect(selectedBounds!.x + selectedBounds!.width).toBeLessThanOrEqual(moreBounds!.x);
  expect(moreBounds!.x + moreBounds!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  await expect(menu).not.toBeVisible();
  await selected.focus();
  await page.keyboard.press('Home');
  await expect(page.getByTestId(`workbench-mainView-tab-${sheets[0].id}`)).toBeFocused();
  await expect(page.getByTestId(`workbench-mainView-tab-${sheets[0].id}`)).toBeInViewport();
  await page.setViewportSize({ width: 2800, height: 900 });
  await expect(more).not.toBeVisible();
  await page.setViewportSize({ width: 1100, height: 900 });
  await expect(more).toBeVisible();
});
