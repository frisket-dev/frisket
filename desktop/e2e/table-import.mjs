import { expect } from '@playwright/test';

export async function importTable(page, projectId, file) {
  await page.getByTestId('ribbon-tab-data').click();
  await page.getByTestId('ribbon-command-import').click();

  await page.getByRole('region', { name: 'Add data', exact: true })
    .locator('input[type="file"]').setInputFiles(file);
  await expect(page.getByRole('region', { name: 'Check rows', exact: true })).toBeVisible();
  const columns = page.getByRole('button', { name: 'Columns →', exact: true });
  await expect(columns).toBeEnabled();
  await columns.click();

  await expect(page.getByRole('table', { name: 'Columns to import' })).toBeVisible();
  await page.getByRole('button', { name: 'Continue →', exact: true }).click();
  const destination = page.locator('.table-import-flow')
    .getByRole('region', { name: 'Import', exact: true });
  await expect(destination).toBeVisible();

  const committed = page.waitForResponse((response) => {
    const pathname = new URL(response.url()).pathname;
    return response.request().method() === 'POST'
      && pathname.startsWith(`/api/projects/${projectId}/import/table-imports/`)
      && pathname.endsWith('/commit');
  });
  await destination.getByRole('button', { name: /^Import(?: |$)/ }).click();
  const response = await committed;
  expect(response.ok()).toBeTruthy();
  const result = await response.json();
  expect(result.status).toBe('accepted');
  expect(Number.isInteger(result.sheet_id)).toBe(true);
  await expect(page.getByTestId('import-workspace-dialog')).toHaveCount(0);
  return result.sheet_id;
}
