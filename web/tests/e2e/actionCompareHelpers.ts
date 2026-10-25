import { expect, type Page } from '@playwright/test';

const ROW_MARKER_WIDTH = 40;
const SOURCE_WIDTH = 140;
const VARIANT_WIDTH = 170;
const GROUP_HEADER_HEIGHT = 28;
const HEADER_HEIGHT = 34;
const ROW_HEIGHT = 34;

async function comparisonCellPoint(page: Page, column: number, row: number) {
  const grid = page.getByTestId('action-compare-grid');
  await expect(grid.locator('canvas').first()).toBeVisible();
  const bounds = await grid.boundingBox();
  if (!bounds) throw new Error('Comparison grid is not visible');
  return {
    x: bounds.x + ROW_MARKER_WIDTH + (column === 0
      ? SOURCE_WIDTH / 2
      : SOURCE_WIDTH + (column - 1) * VARIANT_WIDTH + VARIANT_WIDTH / 2),
    y: bounds.y + GROUP_HEADER_HEIGHT + HEADER_HEIGHT + row * ROW_HEIGHT + ROW_HEIGHT / 2,
  };
}

/** Copy a visible cell through the real grid's clipboard path. Fixtures have
 * one 140px source column, followed by 170px variant columns. */
export async function expectComparisonCell(
  page: Page, variantColumn: number, row: number, expected: string,
): Promise<void> {
  const point = await comparisonCellPoint(page, variantColumn + 1, row);
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.evaluate(() => navigator.clipboard.writeText(''));
  await page.mouse.click(point.x, point.y);
  await page.keyboard.press('ControlOrMeta+C');
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(expected);
}

/** Activate a visible source or variant cell through Glide's native canvas
 * path. Column 0 is the source; later columns are the currently visible
 * comparison variants in display order. */
export async function activateComparisonCell(
  page: Page, column: number, row: number,
): Promise<void> {
  const point = await comparisonCellPoint(page, column, row);
  await page.mouse.dblclick(point.x, point.y);
}
