import { expect, test } from '@playwright/test';
import { clickCell, createProject, importCsv, setColumnType, sheetColumns, uniqueName } from './helpers';

const svg = `
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="80">
  <rect width="100" height="80" fill="white"/>
  <circle cx="42" cy="30" r="18" fill="#8eb7e8"/>
</svg>`;

async function redPixelsInPhotoCell(page: import('@playwright/test').Page): Promise<number> {
  return page.locator('[data-testid="grid"] canvas').first().evaluate((canvas) => {
    const c = canvas as HTMLCanvasElement;
    const rect = c.getBoundingClientRect();
    const sx = c.width / rect.width;
    const sy = c.height / rect.height;
    const ctx = c.getContext('2d');
    if (!ctx) return 0;
    const x = Math.round(40 * sx);
    const y = Math.round(34 * sy);
    const w = Math.round(140 * sx);
    const h = Math.round(88 * sy);
    const pixels = ctx.getImageData(x, y, w, h).data;
    let red = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      if (pixels[i] > 180 && pixels[i + 1] < 100 && pixels[i + 2] < 100 && pixels[i + 3] > 150) {
        red += 1;
      }
    }
    return red;
  });
}

test('image cells draw sibling face bbox overlays on the grid canvas', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('frisket:row-height', '88'));
  const pid = await createProject(page.request, uniqueName('e2e-region-overlays'));
  const imageUrl = `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`;
  const faces = JSON.stringify([{ x: 20, y: 16, w: 42, h: 28 }]).replace(/"/g, '""');
  const sheetId = await importCsv(
    page.request,
    pid,
    'faces.csv',
    `photo,faces\n"${imageUrl}","${faces}"\n`,
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const photo = columns.find((c) => c.name === 'photo');
  expect(photo).toBeTruthy();
  await setColumnType(page.request, pid, photo!.id, 'image');

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await expect
    .poll(() => redPixelsInPhotoCell(page), { timeout: 10_000 })
    .toBeGreaterThan(20);
});

test('row drawer image fields show the same sibling face bbox overlays', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('frisket:row-height', '88'));
  const pid = await createProject(page.request, uniqueName('e2e-row-region-overlays'));
  const imageUrl = `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`;
  const faces = JSON.stringify([{ x: 20, y: 16, w: 42, h: 28 }]).replace(/"/g, '""');
  const sheetId = await importCsv(
    page.request,
    pid,
    'faces.csv',
    `photo,faces\n"${imageUrl}","${faces}"\n`,
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const photo = columns.find((c) => c.name === 'photo');
  expect(photo).toBeTruthy();
  await setColumnType(page.request, pid, photo!.id, 'image');

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await clickCell(page, columns, 'photo', 0);
  await page.keyboard.press('Enter');

  await expect(page.getByTestId('row-drawer')).toBeVisible();
  const regionImage = page.getByTestId('row-region-image');
  await expect(regionImage).toBeVisible();
  const box = page.getByTestId('row-region-box').first();
  await expect(box).toBeVisible();
  await expect.poll(async () => {
    const bounds = await box.boundingBox();
    return bounds ? Math.round(bounds.width * bounds.height) : 0;
  }, { timeout: 10_000 }).toBeGreaterThan(100);
});
