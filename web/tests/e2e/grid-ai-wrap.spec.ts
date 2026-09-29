import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, listSheets, openProject, patchColumn, uniqueName } from './helpers';
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

async function resizeColumnTo(page: Page, columnName: string, targetWidth: number): Promise<number> {
  const grid = page.getByTestId('grid');
  const column = page.getByTestId(`grid-column-${columnName}`);
  const gridBox = await grid.boundingBox();
  const initial = await column.boundingBox();
  if (!gridBox || !initial) throw new Error(`${columnName} column not visible`);
  for (const edgeOffset of [0, -1, 1, -2, 2]) {
    const current = await column.boundingBox();
    if (!current) throw new Error(`${columnName} column not visible`);
    const borderX = current.x + current.width + edgeOffset;
    const headerY = gridBox.y + 17;
    await page.mouse.move(borderX, headerY);
    await page.mouse.down();
    await page.mouse.move(borderX + targetWidth - current.width, headerY, { steps: 8 });
    await page.mouse.up();
    try {
      await expect.poll(async () => (await column.boundingBox())?.width ?? 0, { timeout: 1_000 })
        .toBeCloseTo(targetWidth, -1);
      return (await column.boundingBox())!.width;
    } catch {
      await page.keyboard.press('Escape');
    }
  }
  throw new Error(`could not resize ${columnName} column`);
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

test('the sample Contracts description soft-wraps after Dispatches renders an empty paragraph', async ({ page }) => {
  const description = 'Emergency ambulance response and overnight crews';
  await page.addInitScript((trackedText) => {
    localStorage.setItem('frisket:wrap-text', '1');
    localStorage.setItem('frisket:row-height', '68');
    const draws: Array<{ text: string; x: number; y: number; width: number }> = [];
    Object.assign(window, { __contractDescriptionDraws: draws, __sawParagraphStory: false });
    const original = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function (value, x, y, ...rest) {
      const text = String(value);
      if (text.includes('Members')) Object.assign(window, { __sawParagraphStory: true });
      if (trackedText.split(' ').some((word) => text.includes(word))) {
        draws.push({ text, x, y, width: this.measureText(text).width });
      }
      return original.call(this, value, x, y, ...rest);
    };
  }, description);

  const pid = await createProject(page.request, uniqueName('grid-contract-description-wrap'));
  const seed = await page.request.post(`/api/projects/${pid}/seed-sample`);
  expect(seed.ok()).toBeTruthy();
  const sheets = await listSheets(page.request, pid);
  const dispatches = sheets.find((sheet) => sheet.name === 'Dispatches');
  const contracts = sheets.find((sheet) => sheet.name === 'Contracts');
  if (!dispatches || !contracts) throw new Error('sample project sheets missing');
  await openProject(page, pid, dispatches.id);

  const wrap = page.getByTestId('toggle-wrap');
  await expect(wrap).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('grid-column-story')).toBeVisible();
  await page.evaluate(() => {
    window.dispatchEvent(new CustomEvent('frisket:reveal-grid-column', {
      detail: { columnName: 'story' },
    }));
  });
  await expect.poll(() => page.evaluate(() =>
    (window as unknown as { __sawParagraphStory: boolean }).__sawParagraphStory,
  )).toBe(true);

  await page.getByTestId(`workbench-mainView-tab-${contracts.id}`).click();
  await expect(page.locator('.sheet-title')).toHaveText('Contracts');
  await expect(page.getByTestId('grid-column-description')).toBeVisible();
  const columnWidth = await resizeColumnTo(page, 'description', 156);
  await wrap.click();
  await expect(wrap).toHaveAttribute('aria-pressed', 'false');
  await page.evaluate(() => {
    (window as unknown as { __contractDescriptionDraws: unknown[] }).__contractDescriptionDraws.length = 0;
  });
  await wrap.click();
  await expect(wrap).toHaveAttribute('aria-pressed', 'true');

  const drawLines = () => page.evaluate(() => {
    const draws = (window as unknown as {
      __contractDescriptionDraws: Array<{ text: string; x: number; y: number; width: number }>;
    }).__contractDescriptionDraws;
    return draws.filter((draw) => draw.y > 34 && draw.y < 110);
  });
  await expect.poll(async () => (await drawLines()).length).toBeGreaterThan(1);
  const lines = await drawLines();
  expect(lines).not.toContainEqual(expect.objectContaining({ text: description }));
  expect(new Set(lines.map((line) => Math.round(line.y))).size).toBeGreaterThan(1);

  expect(Math.max(...lines.map((line) => line.width))).toBeLessThan(columnWidth);
});
