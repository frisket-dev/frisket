import { expect, test } from '@playwright/test';
import {
  createProject, importCsv, openFriendlyFilterSidebar, openImportWorkspace,
  previewTableFile, reviewTableColumns, setColumnType, sheetColumns,
} from './helpers';

test('source type and cadence selects fill their flex rows', async ({ page }) => {
  const pid = await createProject(page.request, 'Select source layout');
  await page.route(`**/api/projects/${pid}/sources`, route => route.fulfill({ json: [{
    id: 1, name: 'Council meetings', kind: 'rss', url: 'https://example.org/feed',
    config: {}, sheet_id: null, schedule: '@daily', enabled: true,
    last_checked_at: null, last_status: 'ok', new_rows_total: 0,
  }] }));
  await page.goto(`/p/${pid}`);
  await page.getByTestId('ribbon-tab-data').click();
  await page.getByTestId('ribbon-command-sources').click();
  const cadence = page.getByTestId('source-sched-1');
  await expect(cadence).toBeVisible();
  const cadenceWidth = await cadence.evaluate(select => {
    const row = select.closest('.source-controls')!;
    const siblings = [...row.children].filter(child => child !== select.parentElement);
    const available = row.getBoundingClientRect().width
      - siblings.reduce((sum, child) => sum + child.getBoundingClientRect().width, 0)
      - parseFloat(getComputedStyle(row).columnGap) * siblings.length;
    return Math.abs(select.getBoundingClientRect().width - available);
  });
  expect(cadenceWidth).toBeLessThan(2);
  await page.getByTestId('source-edit-1').click();
  const kind = page.getByTestId('source-kind');
  await expect(kind).toBeVisible();
  expect(await kind.evaluate(select => Math.abs(
    select.getBoundingClientRect().width
    - select.closest('.sources-form-row')!.getBoundingClientRect().width,
  ))).toBeLessThan(2);
});

test('import encoding focus is not clipped and mapping controls stay within their shells', async ({ page }) => {
  const pid = await createProject(page.request, 'Select import layout');
  const sheet = await importCsv(page.request, pid, 'people.csv', 'id,name\na,Ada\n');
  await page.goto(`/p/${pid}/s/${sheet}`);
  await openImportWorkspace(page);
  await previewTableFile(page, {
    name: 'updates.csv', mimeType: 'text/csv', buffer: Buffer.from('id,name\na,Augusta\n'),
  });
  const encoding = page.getByLabel('Character encoding');
  await expect(encoding).toBeVisible();
  await encoding.focus();
  await expect(encoding).toBeFocused();
  await expect(encoding).toHaveCSS('outline-width', '2px');
  await expect(encoding.locator('..')).toHaveCSS('overflow', 'visible');
  await reviewTableColumns(page);
  await page.getByRole('radio', { name: 'Add to an existing sheet' }).check();
  await page.getByLabel('Sheet', { exact: true }).selectOption(String(sheet));
  const mapping = page.getByLabel('Map id');
  await expect(mapping).toBeVisible();
  await expect(mapping).toHaveValue(/^existing:/);
  expect(await mapping.evaluate(select => {
    const shell = select.parentElement!;
    return Math.abs(shell.getBoundingClientRect().width - select.getBoundingClientRect().width);
  })).toBeLessThan(2);
});

test('relative-date select shell is not styled as its caption', async ({ page }) => {
  const pid = await createProject(page.request, 'Select relative date layout');
  const sheet = await importCsv(page.request, pid, 'dates.csv', 'published\n2026-10-01\n');
  const columns = await sheetColumns(page.request, pid, sheet);
  await setColumnType(page.request, pid, columns[0].id, 'date');
  await page.goto(`/p/${pid}/s/${sheet}`);
  await openFriendlyFilterSidebar(page, columns, 'published');
  await page.getByTestId('friendly-facet-published').getByRole('button', { name: 'More filter options' }).click();
  await page.getByTestId('facet-advanced-operator-published').selectOption('date_relative');
  const shell = page.getByTestId('facet-relative-unit-published').locator('..');
  await expect(shell).toHaveCSS('display', 'grid');
  await expect(shell).toHaveCSS('padding-left', '0px');
  await expect(shell).toHaveCSS('border-right-width', '0px');
  await expect(shell.locator('..').locator(':scope > span').first()).toHaveCSS('border-right-width', '1px');
});
