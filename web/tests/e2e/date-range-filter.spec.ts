import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openFriendlyFilterSidebar,
  openProject,
  openToolbarOverflow,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

const rangeFilter = {
  published: {
    between: {
      start: '2026-01-01',
      end: '2026-03-31',
    },
  },
};

function dateCsv(): string {
  const rows: string[] = ['title,published'];
  for (let i = 0; i < 525; i += 1) {
    let title = `outside-${i}`;
    let published = '2025-12-31';
    if (i === 1) {
      title = 'first-day';
      published = '2026-01-01';
    } else if (i === 399) {
      title = 'middle';
      published = '2026-02-15';
    } else if (i === 524) {
      title = 'tail';
      published = '2026-03-31';
    }
    rows.push(`${title},${published}`);
  }
  return `${rows.join('\n')}\n`;
}

function isDateRangeDataRequest(url: URL, pid: string, sheetId: number): boolean {
  if (url.pathname !== `/api/projects/${pid}/sheets/${sheetId}/data`) return false;
  const rawFilter = url.searchParams.get('filter');
  if (rawFilter === null) return false;
  return JSON.stringify(JSON.parse(rawFilter)) === JSON.stringify(rangeFilter);
}

test('date-range filters use date columns and round-trip through saved views', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-date-range-filter'));
  const sheetId = await importCsv(page.request, pid, 'dates.csv', dateCsv());
  const importedColumns = await sheetColumns(page.request, pid, sheetId);
  const publishedColumn = importedColumns.find((column) => column.name === 'published');
  expect(publishedColumn).toBeTruthy();
  await setColumnType(page.request, pid, publishedColumn!.id, 'date');

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Date ranges live in the friendly sidebar alongside the other facets.
  await openFriendlyFilterSidebar(page, importedColumns, 'published');
  await page.getByTestId('facet-range-end-published').fill('2026-03-31');

  const filteredResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    return isDateRangeDataRequest(new URL(res.url()), pid, sheetId);
  });
  await page.getByTestId('facet-range-start-published').fill('2026-01-01');
  const filtered = await (await filteredResponse).json();
  const titleColumn = filtered.columns.find((column: { name: string }) => column.name === 'title');
  expect(titleColumn).toBeTruthy();
  expect(filtered.total).toBe(3);
  expect(
    filtered.rows.map((row: { cells: Record<string, unknown> }) =>
      row.cells[String(titleColumn.id)],
    ),
  ).toEqual(['first-day', 'middle', 'tail']);
  await expect(page.getByTestId('active-grid-filter')).toContainText(
    'published between 2026-01-01 and 2026-03-31',
  );
  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  await expect(page.getByTestId('views-panel')).toBeVisible();
  await page.getByTestId('create-saved-view').click();
  await page.getByTestId('view-name-input').fill('Q1 dates');
  const savedViewResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views`) &&
    res.request().method() === 'POST' &&
    res.status() === 200,
  );
  await page.getByTestId('save-view-button').click();
  const savedView = await (await savedViewResponse).json();
  expect(savedView.spec.filter).toEqual(rangeFilter);

  const clearedResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      !url.searchParams.has('filter')
    );
  });
  await page.getByTestId('clear-grid-filter').click();
  await clearedResponse;
  await expect(page.getByTestId('active-grid-filter')).toBeHidden();

  const savedViewApplyResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    return isDateRangeDataRequest(new URL(res.url()), pid, sheetId);
  });
  await page.getByTestId('apply-saved-view').filter({ hasText: 'Q1 dates' }).click();
  const restored = await (await savedViewApplyResponse).json();
  expect(restored.total).toBe(3);

  await openFriendlyFilterSidebar(page, importedColumns, 'published');
  await expect(page.getByTestId('facet-range-start-published')).toHaveValue('2026-01-01');
  await expect(page.getByTestId('facet-range-end-published')).toHaveValue('2026-03-31');
});

test('relative date filters stay relative on the wire', async ({ page }) => {
  const today = new Date().toISOString().slice(0, 10);
  const pid = await createProject(page.request, uniqueName('e2e-relative-date-filter'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'relative-dates.csv',
    `title,published\ntoday,${today}\nold,2000-01-01\n`,
  );
  const importedColumns = await sheetColumns(page.request, pid, sheetId);
  const publishedColumn = importedColumns.find((column) => column.name === 'published');
  expect(publishedColumn).toBeTruthy();
  await setColumnType(page.request, pid, publishedColumn!.id, 'date');

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openFriendlyFilterSidebar(page, importedColumns, 'published');
  const filedFacet = page.getByTestId('friendly-facet-published');
  await filedFacet.getByRole('button', { name: 'More filter options' }).click();
  await page.getByTestId('facet-advanced-operator-published').selectOption('date_relative');
  await expect(page.getByTestId('facet-relative-amount-published')).toHaveValue('30');
  await expect(page.getByTestId('facet-relative-unit-published')).toHaveValue('days');
  const relativeResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    if (url.pathname !== `/api/projects/${pid}/sheets/${sheetId}/data`) return false;
    const rawFilter = url.searchParams.get('filter');
    return rawFilter !== null && JSON.stringify(JSON.parse(rawFilter)) === JSON.stringify({
      published: { date_relative: { amount: 30, unit: 'days' } },
    });
  });
  await page.getByTestId('facet-advanced-apply-published').click();
  const response = await relativeResponse;
  expect(response.status()).toBe(200);
  expect((await response.json()).total).toBe(1);
  await expect(page.getByTestId('active-grid-filter')).toContainText(
    'published in the last 30 days',
  );
});
