import { expect, test } from '@playwright/test';

import { createProject, importCsv, uniqueName } from './helpers';

test('save-to input exposes its native combobox and keyboard-real rich popup', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-save-to-a11y'));
  const columnNames = Array.from({ length: 18 }, (_, index) => `column_${String(index + 1).padStart(2, '0')}`);
  const csv = `${columnNames.join(',')}\n${columnNames.map((_, index) => `value_${index + 1}`).join(',')}\n`;
  const sheetId = await importCsv(page.request, pid, 'save-to-a11y.csv', csv);

  // A generated Classify output may target generated category columns only.
  // This keyboard-only fixture supplies those metadata facts; it never runs
  // an action or overrides the form's actual compatibility policy.
  await page.route(`**/api/projects/${pid}/sheets/${sheetId}/data*`, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.columns = body.columns.map((column: Record<string, unknown>) => ({
      ...column, type: 'category', ai_generated: true, generation_managed: true,
    }));
    await route.fulfill({ response, json: body });
  });
  await page.goto(`/p/${pid}/s/${sheetId}/action/map.classify`);
  const input = page.getByTestId('field-output-category');
  await expect(input).toBeVisible();
  await expect(input).toHaveAccessibleName('Save to');
  await expect(input).toHaveRole('combobox');
  await expect(input).toHaveAttribute('list', 'field-output-category-options');
  await expect(input).not.toHaveAttribute('role');

  await input.fill('');
  await input.focus();
  await page.keyboard.press('ArrowDown');
  const listbox = page.getByTestId('field-output-category-listbox');
  await expect(listbox).toBeVisible();
  await expect(input).toHaveAttribute('aria-activedescendant', /\S+/);

  for (let index = 0; index < 14; index += 1) {
    await page.keyboard.press('ArrowDown');
  }
  const activeId = await input.getAttribute('aria-activedescendant');
  expect(activeId).toBeTruthy();
  const active = page.locator(`#${activeId}`);
  await expect(active).toHaveClass(/is-active/);
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
