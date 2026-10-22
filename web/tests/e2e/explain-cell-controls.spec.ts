import { expect, test } from '@playwright/test';
import { createProject, importCsv, openCellDrawer, openProject, sheetColumns, uniqueName } from './helpers';

test('explain cell icon remains visible inside the compact action rail', async ({ page }, testInfo) => {
  const pid = await createProject(page.request, uniqueName('explain-icon'));
  const sid = await importCsv(page.request, pid, 'records.csv', 'name\nAda\n');
  const response = await page.request.post(`/api/projects/${pid}/actions/v1/run`, { data: {
    action_id: 'map.template', scope: { kind: 'sheet_rows', sheet_id: sid },
    params: { template: { text: '{{name}}' } }, output_names: { rendered: 'label' },
    idempotency_key: 'explain-icon-template',
  } });
  expect(response.ok()).toBeTruthy();
  expect((await response.json()).status).toBe('completed');
  const columns = await sheetColumns(page.request, pid, sid);
  await openProject(page, pid, sid);
  await openCellDrawer(page, columns, 'label', 0);
  const field = page.getByTestId('row-field-label');
  await field.hover();
  const explain = field.getByRole('button', { name: 'Explain label cell' });
  await expect(explain).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('explain-cell-icon.png') });
  const glyph = await explain.locator('svg').boundingBox();
  expect(glyph).not.toBeNull();
  expect(glyph!.width).toBeGreaterThanOrEqual(12);
  expect(glyph!.height).toBeGreaterThanOrEqual(12);
  await explain.click();
  const panel = page.getByTestId('explain-panel');
  await expect(panel).toBeVisible();
  const before = (await panel.boundingBox())!;
  await page.mouse.move(before.x + before.width - 3, before.y + before.height - 3);
  await page.mouse.down();
  await page.mouse.move(before.x + before.width + 90, before.y + before.height + 70, { steps: 10 });
  await page.mouse.up();
  const after = (await panel.boundingBox())!;
  expect(after.width).toBeGreaterThan(before.width + 50);
  expect(after.height).toBeGreaterThan(before.height + 40);
  expect(after.x + after.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  expect(after.y + after.height).toBeLessThanOrEqual(page.viewportSize()!.height);
  await page.screenshot({ path: testInfo.outputPath('explain-cell-resized.png') });
  await page.setViewportSize({ width: 720, height: 600 });
  await expect.poll(async () => {
    const box = await panel.boundingBox();
    return Boolean(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= 720 && box.y + box.height <= 600);
  }).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('explain-cell-narrow.png') });
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('explain-panel')).toHaveCount(0);
  await expect(explain).toBeFocused();
  await page.setViewportSize({ width: 1600, height: 500 });
  await field.hover();
  await explain.click();
  await expect(panel).toBeVisible();
  await expect(panel).toHaveCSS('resize', 'horizontal');
  const above = (await panel.boundingBox())!;
  const trigger = (await explain.boundingBox())!;
  expect(above.y + above.height).toBeLessThanOrEqual(trigger.y);
  await page.mouse.move(above.x + above.width - 3, above.y + above.height - 3);
  await page.mouse.down();
  await page.mouse.move(above.x + above.width + 60, above.y + above.height + 50, { steps: 10 });
  await page.mouse.up();
  const resizedAbove = (await panel.boundingBox())!;
  expect(resizedAbove.width).toBeGreaterThan(above.width + 30);
  expect(resizedAbove.height).toBeCloseTo(above.height, 0);
  await page.screenshot({ path: testInfo.outputPath('explain-cell-above.png') });
  await panel.getByRole('button', { name: 'Close explain panel' }).click();
  await expect(panel).toHaveCount(0);
});
