import { expect, test } from '@playwright/test';

import { createProject, importCsv, uniqueName } from './helpers';

test('save-to input exposes its native combobox and keyboard-real rich popup', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-save-to-a11y'));
  const columnNames = Array.from({ length: 18 }, (_, index) => `column_${String(index + 1).padStart(2, '0')}`);
  const csv = `${columnNames.join(',')}\n${columnNames.map((_, index) => `value_${index + 1}`).join(',')}\n`;
  const sheetId = await importCsv(page.request, pid, 'save-to-a11y.csv', csv);

  // A Clean result may target generated text columns only. This keyboard-only
  // fixture supplies those metadata facts; it never runs an action or
  // overrides the form's actual compatibility policy.
  const generated = (column: Record<string, unknown>) => ({
    ...column, type: 'text', generated: true, generation_managed: true,
  });
  await page.route((url) => url.pathname === `/api/projects/${pid}/sheets`, async (route) => {
    const response = await route.fetch();
    const sheets: Array<{ id: number; columns: Array<Record<string, unknown>> }> = await response.json();
    await route.fulfill({
      response,
      json: sheets.map((sheet) => (
        sheet.id === sheetId ? { ...sheet, columns: sheet.columns.map(generated) } : sheet
      )),
    });
  });
  await page.goto(`/p/${pid}/s/${sheetId}/action/map.clean_column`);
  const input = page.getByTestId('field-output-cleaned');
  await expect(input).toBeVisible();
  await expect(input).toHaveAccessibleName('Result');
  await expect(input).toHaveRole('combobox');
  await expect(input).toHaveAttribute('list', 'field-output-cleaned-options');
  await expect(input).not.toHaveAttribute('role');

  await input.fill('');
  await input.focus();
  await page.keyboard.press('ArrowDown');
  const listbox = page.getByTestId('field-output-cleaned-listbox');
  await expect(listbox).toBeVisible();
  await expect(input).toHaveAttribute('aria-activedescendant', /\S+/);

  for (let index = 0; index < 14; index += 1) {
    await page.keyboard.press('ArrowDown');
  }
  const activeId = await input.getAttribute('aria-activedescendant');
  expect(activeId).toBeTruthy();
  const active = page.locator(`#${activeId}`);
  await expect(active).toHaveAttribute('data-active', 'true');
  const [listboxBox, activeBox] = await Promise.all([listbox.boundingBox(), active.boundingBox()]);
  expect(listboxBox).not.toBeNull();
  expect(activeBox).not.toBeNull();
  expect(activeBox!.y).toBeGreaterThanOrEqual(listboxBox!.y);
  expect(activeBox!.y + activeBox!.height).toBeLessThanOrEqual(listboxBox!.y + listboxBox!.height + 1);

  const committedValue = await active.locator('span').first().textContent();
  await page.keyboard.press('Enter');
  await expect(input).toHaveValue(committedValue!.trim());
  await expect(input).toBeFocused();
  await expect(input).toHaveAttribute('aria-expanded', 'false');
});
