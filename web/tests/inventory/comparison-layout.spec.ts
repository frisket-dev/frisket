import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject } from '../e2e/helpers';
import { observeInventoryCatalog, waitForInventoryCatalog, waitForInventoryComparison } from './readiness';

test('narrow comparison toolbars keep engines above controls', async ({ page, context }) => {
  observeInventoryCatalog(page);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await context.route('**/*', async route => {
    if (new URL(route.request().url()).origin === process.env.FRISKET_INVENTORY_APP_URL) return route.continue();
    await route.abort();
    throw new Error(`Unexpected external comparison layout request: ${route.request().url()}`);
  });
  const projectId = await createProject(page.request, 'Narrow comparison toolbar layout');
  const sheetId = await importCsv(page.request, projectId, 'stories.csv', 'story\nCouncil approved the contract\n');

  for (const prefix of ['ocr-compare', 'transcribe-compare', 'translate-compare', 'topic-compare']) {
    await test.step(prefix, async () => {
      // Navigate through the real ribbon at desktop width, then measure the
      // actual comparison at the responsive breakpoint without executing it.
      await page.setViewportSize({ width: 1440, height: 1000 });
      await openProject(page, projectId, sheetId);
      await waitForInventoryCatalog(page);
      const command = page.getByTestId(`ribbon-command-${prefix}`);
      const tabs = page.getByTestId('act-ribbon').getByRole('tab');
      for (let index = 0; index < await tabs.count() && !await command.isVisible(); index++) await tabs.nth(index).click();
      await command.click();
      await waitForInventoryComparison(page, prefix);
      await page.setViewportSize({ width: 720, height: 900 });
      await page.evaluate(() => document.fonts.ready);

      const toolbar = page.getByTestId(`${prefix}-toolbar`);
      const chips = toolbar.getByTestId(`${prefix}-chips-row`);
      const controls = toolbar.getByTestId(`${prefix}-controls-row`);
      await expect(chips).toBeVisible();
      await expect(controls).toBeVisible();
      await expect.poll(async () => {
        const [enginesBox, controlsBox] = await Promise.all([chips.boundingBox(), controls.boundingBox()]);
        return Boolean(enginesBox && controlsBox && enginesBox.y + enginesBox.height <= controlsBox.y + 1);
      }, { message: `${prefix}: engine selection occupies the row above run controls at 720px` }).toBe(true);
      expect(await toolbar.evaluate((element, id) => {
        const enginesRow = element.querySelector(`[data-testid="${id}-chips-row"]`)!;
        const controlsRow = element.querySelector(`[data-testid="${id}-controls-row"]`)!;
        return Boolean(enginesRow.compareDocumentPosition(controlsRow) & Node.DOCUMENT_POSITION_FOLLOWING);
      }, prefix), `${prefix}: engine selection precedes controls in DOM order`).toBe(true);
    });
  }
  expect(errors, 'Uncaught comparison layout browser errors').toEqual([]);
});
