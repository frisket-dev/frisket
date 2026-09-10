import { expect, test } from '@playwright/test';
import { createProject, importCsv, revealRibbonAction, uniqueName } from './helpers';

test('action launcher renders generated cards from v1 catalog when legacy metadata is blocked', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-v1-action-catalog-resilience'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'note\n"fee was $96 flat"\n');

  await page.route('**/api/recipes', async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'legacy metadata unavailable' }),
    });
  });

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // The Act ribbon replaced the retired discovery tiles as the launcher
  // (workbench-ia-action-drawer-v1). With the legacy /api/recipes metadata
  // endpoint blocked (500), the launcher must still render from the v1
  // catalog. map.clean_column has NO static template — its ribbon tile exists only
  // because the v1 catalog generated it, which is the resilience proof the
  // retired generated discovery cards asserted.
  const geocodeTile = await revealRibbonAction(page, 'enrich.geocode');
  await expect(geocodeTile).toContainText('Geocode');

  const cleanColumnTile = await revealRibbonAction(page, 'map.clean_column');
  await expect(cleanColumnTile).toContainText('Clean column');

  const regexTile = await revealRibbonAction(page, 'map.regex_extract');
  await expect(regexTile).toContainText('Regex extract');
});
