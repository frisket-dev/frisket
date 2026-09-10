// Grid theme-flip continuity.
//
// Flipping the user-facing appearance toggle (AccountMenu ▸ Appearance ▸
// Light/Dark, which sets data-frisket-theme on <html>) must NOT reset the
// grid's scroll position, cell/row selection, or column widths, and must not
// leave an open header caret menu in a broken state — while the grid canvas
// STILL repaints to the new palette live (the MutationObserver-driven
// gridTheme repaint stays). The regression: SheetGrid rendered <DataEditor>
// with a key derived from the theme colors, so a flip remounted glide and
// wiped its internal scroll/selection wholesale.

import { expect, test, type Page } from '@playwright/test';
import {
  clickCell,
  clickHeaderMenu,
  createProject,
  importCsv,
  openProject,
  selectRow,
  sheetColumns,
  uniqueName,
  type WireColumn,
} from './helpers';

const COL_COUNT = 14;
const ROW_COUNT = 80;

function seedCsv(): string {
  const header = Array.from({ length: COL_COUNT }, (_, c) => `c${String(c).padStart(2, '0')}`);
  const lines = [header.join(',')];
  for (let r = 0; r < ROW_COUNT; r++) {
    lines.push(header.map((name) => `${name}-r${r}`).join(','));
  }
  return `${lines.join('\n')}\n`;
}

async function seedWideGrid(page: Page): Promise<WireColumn[]> {
  const pid = await createProject(page.request, uniqueName('grid-theme-flip'));
  const sheetId = await importCsv(page.request, pid, 'wide.csv', seedCsv());
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  return columns;
}

/** Read glide's DOM scroll element offsets (the canvas scroll lives here). */
async function readScroll(page: Page): Promise<{ left: number; top: number }> {
  return page.evaluate(() => {
    const el = document.querySelector<HTMLElement>('.dvn-scroller');
    return { left: el?.scrollLeft ?? -1, top: el?.scrollTop ?? -1 };
  });
}

/** Total scrollable content width — a stable proxy for the column widths. */
async function readContentWidth(page: Page): Promise<number> {
  return page.evaluate(() => document.querySelector<HTMLElement>('.dvn-scroller')?.scrollWidth ?? -1);
}

type Selection = { cell: [number, number] | null; rows: number[] };
async function readSelection(page: Page): Promise<Selection> {
  return page.evaluate(() => {
    const sel = (window as unknown as { __frisketGridSelection?: Selection }).__frisketGridSelection;
    return sel ?? { cell: null, rows: [] };
  });
}

/** Average color of a small block of the largest grid canvas — used to prove
 *  the canvas actually repainted to the new palette (not just the CSS token). */
async function sampleCanvas(page: Page): Promise<number> {
  return page.evaluate(() => {
    const canvases = Array.from(
      document.querySelectorAll<HTMLCanvasElement>('[data-testid="grid"] canvas'),
    );
    if (canvases.length === 0) return -1;
    const canvas = canvases.sort((a, b) => b.width * b.height - a.width * a.height)[0];
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) return -1;
    const { data } = ctx.getImageData(40, 40, 8, 8);
    let sum = 0;
    for (let i = 0; i < data.length; i++) sum += data[i];
    return sum;
  });
}

/** Drive the REAL user-facing toggle: AccountMenu ▸ Appearance ▸ Light/Dark. */
async function flipThemeViaMenu(page: Page, to: 'light' | 'dark'): Promise<void> {
  await page.getByTestId('chrome-account').click();
  await expect(page.getByTestId('account-menu')).toBeVisible();
  await page.getByTestId('account-appearance').click();
  await page.getByTestId(`account-appearance-${to}`).click();
  await expect
    .poll(() => page.evaluate(() => document.documentElement.dataset.frisketTheme))
    .toBe(to);
}

async function currentTheme(page: Page): Promise<'light' | 'dark'> {
  const t = await page.evaluate(() => document.documentElement.dataset.frisketTheme);
  return t === 'dark' ? 'dark' : 'light';
}

async function scrollBothAxes(page: Page): Promise<{ left: number; top: number }> {
  const box = await page.getByTestId('grid').boundingBox();
  if (!box) throw new Error('grid not visible');
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(700, 1200);
  // glide uses smooth scroll; wait for the offset to settle above zero.
  await expect.poll(async () => (await readScroll(page)).top).toBeGreaterThan(100);
  await expect.poll(async () => (await readScroll(page)).left).toBeGreaterThan(100);
  return readScroll(page);
}

test('grid scroll offset survives an appearance theme flip', async ({ page }) => {
  await seedWideGrid(page);
  const before = await scrollBothAxes(page);
  const paintBefore = await sampleCanvas(page);

  const to = (await currentTheme(page)) === 'dark' ? 'light' : 'dark';
  await flipThemeViaMenu(page, to);

  // The canvas must actually repaint to the new palette (live theme flip stays).
  await expect.poll(() => sampleCanvas(page)).not.toBe(paintBefore);

  // ...and the scroll offset must be preserved (the regression reset it to 0).
  const after = await readScroll(page);
  expect(Math.abs(after.top - before.top)).toBeLessThanOrEqual(2);
  expect(Math.abs(after.left - before.left)).toBeLessThanOrEqual(2);
});

test('grid cell selection survives an appearance theme flip', async ({ page }) => {
  const columns = await seedWideGrid(page);
  await clickCell(page, columns, 'c02', 3);
  await expect.poll(async () => (await readSelection(page)).cell).not.toBeNull();
  const before = await readSelection(page);
  expect(before.cell).not.toBeNull();

  const to = (await currentTheme(page)) === 'dark' ? 'light' : 'dark';
  await flipThemeViaMenu(page, to);

  const after = await readSelection(page);
  expect(after.cell).toEqual(before.cell);
});

test('grid multi-row selection survives an appearance theme flip', async ({ page }) => {
  await seedWideGrid(page);
  await selectRow(page, 2);
  await selectRow(page, 4);
  await selectRow(page, 6);
  await expect.poll(async () => (await readSelection(page)).rows.length).toBeGreaterThanOrEqual(3);
  const before = (await readSelection(page)).rows.slice().sort((a, b) => a - b);

  const to = (await currentTheme(page)) === 'dark' ? 'light' : 'dark';
  await flipThemeViaMenu(page, to);

  const after = (await readSelection(page)).rows.slice().sort((a, b) => a - b);
  expect(after).toEqual(before);
});

test('grid column widths survive an appearance theme flip', async ({ page }) => {
  const columns = await seedWideGrid(page);
  // Widen the first column by dragging its right-edge resize handle.
  const box = await page.getByTestId('grid').boundingBox();
  if (!box) throw new Error('grid not visible');
  const MARKER = 40;
  const firstWidth = columns[0].type === 'text' ? 240 : 140;
  const borderX = box.x + MARKER + firstWidth;
  const y = box.y + 17; // header band
  await page.mouse.move(borderX, y);
  await page.mouse.down();
  await page.mouse.move(borderX + 90, y, { steps: 8 });
  await page.mouse.up();

  const before = await readContentWidth(page);
  expect(before).toBeGreaterThan(0);

  const to = (await currentTheme(page)) === 'dark' ? 'light' : 'dark';
  await flipThemeViaMenu(page, to);

  const after = await readContentWidth(page);
  expect(Math.abs(after - before)).toBeLessThanOrEqual(2);
});

test('open column caret menu stays graceful across an appearance theme flip', async ({ page }) => {
  const columns = await seedWideGrid(page);
  await clickHeaderMenu(page, columns, 'c01');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();

  const to = (await currentTheme(page)) === 'dark' ? 'light' : 'dark';
  await flipThemeViaMenu(page, to);

  // Whatever the flip does to the open menu, the grid stays interactive and the
  // caret menu remains reachable + functional afterward (no orphaned overlay).
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('grid-column-header-menu')).toBeHidden();
  await expect(page.getByTestId('grid')).toBeVisible();
  await clickHeaderMenu(page, columns, 'c01');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
});
