import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';
import { seedReviewClassifyRun } from './reviewFixtures';

test('review pages stable row bundles without loading the whole sheet or dropping decided rows', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-review-paging'));
  const csv = 'story\n' + Array.from({ length: 26 }, (_, index) => `Story ${index + 1}`).join('\n');
  const sheetId = await importCsv(page.request, pid, 'stories.csv', csv);
  seedReviewClassifyRun({ pid, sheetId, sourceColumns: ['story'], context: 'Classify the story.',
    fields: [{ name: 'beat', type: 'category', labels: ['civic'] }], reply: { beat: 'civic' } });
  // Random order samples bundles with a POST body; ordered paging is a GET.
  const queries: Array<Record<string, unknown>> = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (!url.pathname.endsWith('/review/bundles')) return;
    queries.push(request.method() === 'POST'
      ? request.postDataJSON() as Record<string, unknown>
      : Object.fromEntries(url.searchParams));
  });
  await openProject(page, pid, sheetId);
  await page.getByTestId('review-queue-button').click();
  const queue = page.getByTestId('review-queue');
  const position = queue.getByTestId('review-page-status');
  await expect(position).toHaveText('1 / 26', { timeout: 20_000 });
  for (let row = 2; row <= 26; row += 1) {
    await queue.getByRole('button', { name: 'Next row', exact: true }).first().click();
    await expect(position).toHaveText(`${row} / 26`, { timeout: 20_000 });
  }
  expect(queries.every((query) => Number(query.limit) === 25 && String(query.include_reviewed) === 'true')).toBe(true);
  // The second page excludes the 25 rows already in hand instead of reloading them.
  expect(queries.some((query) => Array.isArray(query.exclude_row_ids) && query.exclude_row_ids.length === 25)).toBe(true);
  await queue.getByRole('button', { name: 'Accept beat' }).click();
  await expect(queue.getByText('Row done', { exact: true })).toBeVisible();
  await expect(position).toHaveText('26 / 26');
  await queue.getByRole('button', { name: 'Previous row' }).click();
  await expect(position).toHaveText('25 / 26');
  await queue.getByRole('button', { name: 'Next row', exact: true }).first().click();
  await expect(position).toHaveText('26 / 26');
  await expect(queue.getByRole('button', { name: 'Accept beat' })).toHaveAttribute('aria-pressed', 'true');
});
