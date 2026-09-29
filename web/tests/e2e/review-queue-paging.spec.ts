import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';
import { seedReviewClassifyRun } from './reviewFixtures';

test('review pages stable row bundles without loading the whole sheet or dropping decided rows', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-review-paging'));
  const csv = 'story\n' + Array.from({ length: 26 }, (_, index) => `Story ${index + 1}`).join('\n');
  const sheetId = await importCsv(page.request, pid, 'stories.csv', csv);
  seedReviewClassifyRun({ pid, sheetId, sourceColumns: ['story'], context: 'Classify the story.',
    fields: [{ name: 'beat', type: 'category', labels: ['civic'] }], reply: { beat: 'civic' } });
  const queries: URL[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith('/review/bundles')) queries.push(url);
  });
  await openProject(page, pid, sheetId);
  await page.getByTestId('review-queue-button').click();
  const queue = page.getByTestId('review-queue');
  const position = queue.getByTestId('review-page-status');
  await expect(position).toHaveText('1 / 26');
  for (let row = 2; row <= 26; row += 1) {
    await queue.getByRole('button', { name: 'Next row', exact: true }).first().click();
    await expect(position).toHaveText(`${row} / 26`);
  }
  expect(queries.map((url) => url.searchParams.get('offset'))).toEqual(['0', '25']);
  expect(queries.every((url) => url.searchParams.get('limit') === '25' && url.searchParams.get('include_reviewed') === 'true')).toBe(true);
  await queue.getByRole('button', { name: 'Accept beat' }).click();
  await expect(queue.getByText('Row done', { exact: true })).toBeVisible();
  await expect(position).toHaveText('26 / 26');
  await queue.getByRole('button', { name: 'Previous row' }).click();
  await expect(position).toHaveText('25 / 26');
  await queue.getByRole('button', { name: 'Next row', exact: true }).first().click();
  await expect(position).toHaveText('26 / 26');
  await expect(queue.getByRole('button', { name: 'Accept beat' })).toHaveAttribute('aria-pressed', 'true');
});
