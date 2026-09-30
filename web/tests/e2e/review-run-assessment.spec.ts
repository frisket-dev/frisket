import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';
import { seedReviewClassifyRun } from './reviewFixtures';

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });

test('review scopes runs and fields through the toolbar, reports decisions, and reopens a completed run', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-run-assessment'));
  const firstSheet = await importCsv(page.request, pid, 'first.csv', 'story\n"First source"\n');
  const secondSheet = await importCsv(page.request, pid, 'second.csv', 'story\n"Second source"\n"Third source"\n');
  for (const [sheetId, label] of [[firstSheet, 'first'], [secondSheet, 'second']] as const) {
    seedReviewClassifyRun({
      pid, sheetId, idempotencyKey: `assessment-${label}`, sourceColumns: ['story'], context: 'Assess source.',
      fields: [{ name: 'beat', type: 'category', labels: ['civic', 'private'] },
        { name: 'tone', type: 'category', labels: ['low', 'high'] }],
      reply: { beat: 'civic', tone: 'high' },
    });
  }
  const result = await page.request.get(`/api/projects/${pid}/review/runs`);
  expect(result.ok()).toBeTruthy();
  const { runs } = await result.json();
  expect(runs).toHaveLength(2);
  const recent = runs.find((run: { sheet_id: number }) => run.sheet_id === secondSheet);
  const older = runs.find((run: { sheet_id: number }) => run.sheet_id === firstSheet);
  const tone = recent.fields.find((field: { column_name: string }) => field.column_name === 'tone');
  const queries: URL[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith('/review/bundles')) queries.push(url);
  });

  await openProject(page, pid, secondSheet);
  await page.getByTestId('review-queue-button').click();
  const queue = page.getByTestId('review-queue');
  await expect(queue.getByTestId('review-run-select')).toHaveValue(String(recent.run_id));
  await expect(queue.getByRole('img', { name: 'No review decisions yet', exact: true })).toBeVisible();
  await expect(queue.getByTestId('review-run-progress')).toContainText('0 of 4 reviewed');
  await queue.getByRole('button', { name: 'Review order' }).click();
  const details = queue.getByTestId('review-order');
  await expect(details.getByRole('button', { name: 'Random', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(details.getByRole('button', { name: 'Lowest confidence', exact: true })).toBeDisabled();
  await queue.getByRole('button', { name: 'Review order' }).click();
  await expect(queue.getByTestId('review-field-beat')).toBeVisible();

  await queue.getByTestId('review-run-select').selectOption(String(older.run_id));
  await expect(queue.getByTestId('review-field-beat')).toBeVisible();
  await queue.getByTestId('review-run-select').selectOption(String(recent.run_id));
  await queue.getByTestId('review-field-select').selectOption(String(tone.column_id));
  await expect(queue.getByTestId('review-field-tone')).toBeVisible();
  await expect(queue.getByTestId('review-field-beat')).toHaveCount(0);
  const scopedQuery = queries.at(-1)!;
  expect(scopedQuery.searchParams.get('run_id')).toBe(String(recent.run_id));
  expect(scopedQuery.searchParams.get('field_id')).toBe(String(tone.column_id));
  expect(scopedQuery.searchParams.get('order')).toBe('shuffle');
  const seed = scopedQuery.searchParams.get('seed');

  await queue.getByRole('button', { name: 'Accept tone', exact: true }).click();
  await queue.getByTestId('review-field-select').selectOption('');
  await expect(queue.getByTestId('review-field-beat')).toBeVisible();
  await queue.getByRole('button', { name: 'Reject beat', exact: true }).click();
  await queue.getByRole('button', { name: 'Results', exact: true }).click();
  await expect(page.getByTestId('review-run-summary')).toContainText('1 accepted');
  await expect(page.getByTestId('review-run-summary')).toContainText('1 incorrect');
  await expect(page.getByTestId('review-run-summary')).toContainText('50% correct among reviewed');
  const resultsPanel = page.getByTestId('review-results');
  expect(await resultsPanel.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  await expect(queue.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '2');
  if (process.env.FRISKET_REVIEW_SCREENSHOTS) {
    await page.screenshot({ path: `${process.env.FRISKET_REVIEW_SCREENSHOTS}/review-results@2x.png` });
  }
  await page.setViewportSize({ width: 620, height: 850 });
  expect(await resultsPanel.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  if (process.env.FRISKET_REVIEW_SCREENSHOTS) {
    await page.screenshot({ path: `${process.env.FRISKET_REVIEW_SCREENSHOTS}/review-results-narrow@2x.png` });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('review-results')).toBeHidden();
  await expect(queue).toBeVisible();
  expect(queries.at(-1)!.searchParams.get('seed')).toBe(seed);
  await expect(queue.getByRole('img', { name: 'Review in progress', exact: true })).toBeVisible();

  // Completing a review must not approve the unchecked sibling output.
  await queue.getByRole('button', { name: 'Mark review complete' }).click();
  await expect(queue.getByRole('button', { name: 'Reopen review' })).toBeVisible();
  await expect(queue.getByRole('img', { name: 'Review complete', exact: true })).toBeVisible();
  await expect(queue.getByRole('button', { name: 'Accept beat', exact: true })).toBeDisabled();
  await expect(queue.getByRole('button', { name: 'Edit beat', exact: true })).toBeDisabled();
  const summary = await page.request.get(`/api/projects/${pid}/review/runs?run_id=${recent.run_id}`);
  const completed = (await summary.json()).runs[0];
  expect(completed.review_status).toBe('complete');
  expect(completed.total.accepted_count).toBe(1);
  expect(completed.total.incorrect_count).toBe(1);
  expect(completed.total.unreviewed_count).toBe(2);

  await queue.getByRole('button', { name: 'Reopen review' }).click();
  await expect(queue.getByRole('button', { name: 'Mark review complete' })).toBeVisible();
  await queue.getByRole('button', { name: 'Results', exact: true }).click();
  await expect(page.getByTestId('review-run-summary')).toContainText('2 reviewed of 4');
});
