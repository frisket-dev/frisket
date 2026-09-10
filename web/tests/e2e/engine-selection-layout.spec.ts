import { expect, test } from '@playwright/test';
import { createProject, importCsv, openAction, uniqueName } from './helpers';

test('Markdown keeps a full-width engine picker when the deployment omits the default', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-markdown-engine'));
  await importCsv(page.request, pid, 'documents.csv', 'html\n"<p>Document</p>"\n');
  await page.route('**/actions/v1/catalog', async (route) => {
    const response = await route.fetch();
    const catalog = await response.json();
    const markdown = catalog.actions.find((action: { kind: string }) => action.kind === 'media.to_markdown');
    // Cloud offers Docling while the public schema still defaults to MarkItDown.
    markdown.ui_hints.engines = [{ id: 'docling', label: 'Docling', tier: 'hosted', available: true }];
    await route.fulfill({ response, json: catalog });
  });
  await page.goto(`/p/${pid}`);
  await openAction(page, 'media.to_markdown');

  const field = page.getByTestId('field-engine');
  const button = field.getByTestId('engine-picker-button');
  await expect(button).toContainText('markitdown (unavailable)');
  const [labelBox, buttonBox, contentWidth] = await Promise.all([
    field.locator('.form-label').boundingBox(),
    button.boundingBox(),
    page.getByTestId('generated-action-form').evaluate((form) => {
      const style = getComputedStyle(form);
      return form.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    }),
  ]);
  expect(labelBox).not.toBeNull();
  expect(buttonBox).not.toBeNull();
  expect(labelBox!.y + labelBox!.height).toBeLessThanOrEqual(buttonBox!.y);
  expect(Math.abs(buttonBox!.width - contentWidth)).toBeLessThanOrEqual(2);

  await button.click();
  const unavailable = page.getByTestId('engine-option-markitdown');
  await expect(unavailable).toHaveAttribute('aria-disabled', 'true');
  await expect(unavailable).toBeDisabled();
  await page.getByTestId('engine-option-docling').click();
  await expect(button).toContainText('Docling');
  await expect(button).toContainText('Hosted');
  await button.click();
  await expect(page.getByTestId('engine-option-docling')).toHaveAttribute('aria-selected', 'true');
});
