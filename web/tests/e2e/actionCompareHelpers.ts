import { expect, type Page } from '@playwright/test';

/** Copy a visible cell through the real grid's clipboard path. Fixtures have
 * one 140px source column, followed by 170px variant columns. */
export async function expectComparisonCell(
  page: Page, variantColumn: number, row: number, expected: string,
): Promise<void> {
  const grid = page.getByTestId('action-compare-grid');
  await expect(grid.locator('canvas').first()).toBeVisible();
  const bounds = await grid.boundingBox();
  if (!bounds) throw new Error('Comparison grid is not visible');
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(() => navigator.clipboard.writeText(''));
  await page.mouse.click(bounds.x + 40 + 140 + variantColumn * 170 + 85,
    bounds.y + 28 + 34 + row * 34 + 17);
  await page.keyboard.press('ControlOrMeta+C');
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(expected);
}
