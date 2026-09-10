import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openProject,
  uniqueName,
} from './helpers';

async function textLineClusters(page: import('@playwright/test').Page): Promise<number> {
  return page.evaluate(() => {
    const grid = document.querySelector<HTMLElement>('[data-testid="grid"]');
    const anchor = document.querySelector<HTMLElement>('[data-testid="grid-column-story"]');
    const canvases = Array.from(grid?.querySelectorAll<HTMLCanvasElement>('canvas') ?? []);
    const canvas = canvases.sort((a, b) => b.width * b.height - a.width * a.height)[0];
    const context = canvas?.getContext('2d', { willReadFrequently: true });
    if (!grid || !anchor || !canvas || !context) return 0;

    const gridRect = grid.getBoundingClientRect();
    const anchorRect = anchor.getBoundingClientRect();
    const canvasRect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / canvasRect.width;
    const scaleY = canvas.height / canvasRect.height;
    const left = Math.round((anchorRect.left - canvasRect.left + 8) * scaleX);
    const right = Math.round((anchorRect.right - canvasRect.left - 8) * scaleX);
    const top = Math.round((gridRect.top - canvasRect.top + 38) * scaleY);
    const bottom = Math.round((gridRect.top - canvasRect.top + 96) * scaleY);
    const data = context.getImageData(left, top, right - left, bottom - top);
    const inkRows: boolean[] = [];
    for (let y = 0; y < data.height; y += 1) {
      let darkPixels = 0;
      for (let x = 0; x < data.width; x += 1) {
        const index = (y * data.width + x) * 4;
        if (
          data.data[index] < 150 &&
          data.data[index + 1] < 150 &&
          data.data[index + 2] < 150 &&
          data.data[index + 3] > 0
        ) {
          darkPixels += 1;
        }
      }
      inkRows.push(darkPixels >= Math.max(3, Math.round(scaleX * 2)));
    }
    let clusters = 0;
    let inCluster = false;
    for (const hasInk of inkRows) {
      if (hasInk && !inCluster) clusters += 1;
      inCluster = hasInk;
    }
    return clusters;
  });
}

async function dragColumnWider(
  page: import('@playwright/test').Page,
  column: import('@playwright/test').Locator,
): Promise<number> {
  const gridBox = await page.getByTestId('grid').boundingBox();
  const initial = await column.boundingBox();
  if (!gridBox || !initial) throw new Error('story column not visible');
  for (const edgeOffset of [0, -1, 1, -2, 2]) {
    const current = await column.boundingBox();
    if (!current) throw new Error('story column not visible');
    const borderX = current.x + current.width + edgeOffset;
    const headerY = gridBox.y + 17;
    await page.mouse.move(borderX, headerY);
    await page.mouse.down();
    await page.mouse.move(borderX + 80, headerY, { steps: 8 });
    await page.mouse.up();
    try {
      await expect.poll(async () => (await column.boundingBox())?.width ?? 0, { timeout: 1_000 })
        .toBeGreaterThan(initial.width);
      return (await column.boundingBox())!.width;
    } catch {
      await page.keyboard.press('Escape');
    }
  }
  throw new Error('column resize handle did not respond');
}

test('wrapped grid text stays wrapped after a column resize and reload', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('grid-wrap-resize'));
  const longText = 'The council approved the riverfront contract after several hours of testimony from neighborhood residents.';
  const sheetId = await importCsv(
    page.request,
    pid,
    'dispatches.csv',
    `story\n"${longText}"\n`,
  );
  await openProject(page, pid, sheetId);

  const wrapToggle = page.getByTestId('toggle-wrap');
  if ((await wrapToggle.getAttribute('aria-pressed')) !== 'true') {
    await wrapToggle.click();
  }
  await expect(page.getByTestId('toggle-wrap')).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => textLineClusters(page)).toBeGreaterThan(1);

  const columnAnchor = page.getByTestId('grid-column-story');
  const resizedWidth = await dragColumnWider(page, columnAnchor);
  await expect.poll(() => textLineClusters(page)).toBeGreaterThan(1);

  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('toggle-wrap')).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(async () => (await page.getByTestId('grid-column-story').boundingBox())?.width ?? 0)
    .toBeCloseTo(resizedWidth, 0);
  await expect.poll(() => textLineClusters(page)).toBeGreaterThan(1);
});
