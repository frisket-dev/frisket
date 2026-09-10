import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openProject, patchColumn, uniqueName } from './helpers';
import { seedGeneratedCellEvidence } from './investigativeActionFixtures';

test.use({ deviceScaleFactor: 2 });

async function firstRowInkLines(page: Page, columnName: string): Promise<number> {
  return page.evaluate((name) => {
    const grid = document.querySelector<HTMLElement>('[data-testid="grid"]');
    const column = document.querySelector<HTMLElement>(`[data-testid="grid-column-${name}"]`);
    const canvases = Array.from(grid?.querySelectorAll<HTMLCanvasElement>('canvas') ?? []);
    const canvas = canvases.sort((a, b) => b.width * b.height - a.width * a.height)[0];
    const context = canvas?.getContext('2d', { willReadFrequently: true });
    if (!grid || !column || !canvas || !context) return 0;
    const gridRect = grid.getBoundingClientRect();
    const columnRect = column.getBoundingClientRect();
    const canvasRect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / canvasRect.width;
    const scaleY = canvas.height / canvasRect.height;
    const left = Math.round((columnRect.left - canvasRect.left + 8) * scaleX);
    const top = Math.round((gridRect.top - canvasRect.top + 38) * scaleY);
    const width = Math.round((columnRect.width - 16) * scaleX);
    const height = Math.round(58 * scaleY);
    const pixels = context.getImageData(left, top, width, height);
    let lines = 0;
    let lastInkRow = -10;
    for (let y = 0; y < pixels.height; y += 1) {
      let ink = 0;
      for (let x = 0; x < pixels.width; x += 1) {
        const offset = (y * pixels.width + x) * 4;
        if (pixels.data[offset] < 150 && pixels.data[offset + 1] < 150 && pixels.data[offset + 2] < 150) ink += 1;
      }
      if (ink >= 3) {
        if (y - lastInkRow > 6) lines += 1;
        lastInkRow = y;
      }
    }
    return lines;
  }, columnName);
}

test('a generated markdown column inherits wrap text before and after reload', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('grid-ai-wrap'));
  const sheetId = await importCsv(page.request, pid, 'notes.csv', 'source\nseed\n');
  const columnName = 'generated_summary';
  const generated = seedGeneratedCellEvidence({
    pid,
    sheetId,
    outputColumnName: columnName,
    outputValue: 'The council approved a long riverfront contract after several hours of public testimony from neighborhood residents.',
    producerKind: 'map.extract',
  });
  await patchColumn(page.request, pid, Number(generated.columnId), { format: 'markdown' });

  await openProject(page, pid, sheetId);
  const wrap = page.getByTestId('toggle-wrap');
  await expect(wrap).toHaveAttribute('aria-pressed', 'false');
  await expect.poll(() => firstRowInkLines(page, columnName)).toBe(1);
  if ((await wrap.getAttribute('aria-pressed')) !== 'true') await wrap.click();
  await expect.poll(() => firstRowInkLines(page, columnName)).toBeGreaterThan(1);

  await page.reload();
  await expect(page.getByTestId('toggle-wrap')).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => firstRowInkLines(page, columnName)).toBeGreaterThan(1);
});

test('a generated plain-text column soft-wraps before canvas drawing', async ({ page }) => {
  const outputValue = 'Daniel Alvarez filed a lawsuit after several hours of public testimony concerning a riverfront construction contract.';
  await page.addInitScript(() => {
    localStorage.setItem('frisket:wrap-text', '0');
    const draws: string[] = [];
    Object.assign(window, { __softWrapDraws: draws });
    const original = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function (value, x, y, ...rest) {
      const text = String(value);
      if (text.includes('Daniel Alvarez')) draws.push(text);
      return original.call(this, value, x, y, ...rest);
    };
  });

  const pid = await createProject(page.request, uniqueName('grid-ai-soft-wrap'));
  const sheetId = await importCsv(page.request, pid, 'notes.csv', 'source\nseed\n');
  seedGeneratedCellEvidence({ pid, sheetId, outputColumnName: 'summary', outputValue, producerKind: 'map.summarize' });

  await openProject(page, pid, sheetId);
  const wrap = page.getByTestId('toggle-wrap');
  await expect(wrap).toHaveAttribute('aria-pressed', 'false');
  await page.evaluate(() => {
    (window as unknown as { __softWrapDraws: string[] }).__softWrapDraws.length = 0;
  });
  await wrap.click();
  const draws = () => page.evaluate(() => (window as unknown as { __softWrapDraws: string[] }).__softWrapDraws);
  await expect.poll(async () => (await draws()).length).toBeGreaterThan(0);
  expect(await draws()).not.toContain(outputValue);
});
