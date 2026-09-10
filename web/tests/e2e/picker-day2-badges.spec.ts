// onboard2-picker-day2-badges-v1: the Home screen cards (§5A) show a relative
// last-modified label and a pending-review count badge (only when nonzero),
// and rows sort by recency. Own projects (mutating); the seeded fixture
// projects ("Local stories" et al.) are long-since created, so a project we
// touch just now is guaranteed to sort above them regardless of what other
// specs are doing in parallel.

import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';
import { seedReviewClassifyRun } from './reviewFixtures';

const CSV = `snippet
"The zoning board approved a variance for the mayor's cousin without discussion."
"Bus ridership rose 4 percent after the new crosstown route opened."
`;

test('picker row shows a relative last-modified label', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-picker-badge'));

  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();
  const row = page.getByTestId(`project-${pid}`);
  await expect(row).toBeVisible();
  await expect(row.getByTestId(`project-${pid}-updated`)).toBeVisible();
  await expect(row.getByTestId(`project-${pid}-updated`)).toHaveText(/now|ago|minute|hour/i);
});

test('picker row hides the pending-review badge until it is nonzero, then shows the count', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-picker-badge'));
  const sheetId = await importCsv(page.request, pid, 'picker-badge.csv', CSV);

  await page.goto('/');
  const row = page.getByTestId(`project-${pid}`);
  await expect(row).toBeVisible();
  await expect(row.getByTestId(`project-${pid}-pending`)).toHaveCount(0);

  seedReviewClassifyRun({
    pid,
    sheetId,
    sourceColumns: ['snippet'],
    context: 'One-line local news items.',
    fields: [
      {
        name: 'beat',
        type: 'category',
        labels: ['corruption', 'transit', 'infrastructure'],
        description: 'best-fit beat',
      },
    ],
    reply: {
      beat: 'corruption',
      beat_justification: 'mentions civic risk',
      beat_confidence: 0.41,
    },
  });

  await page.goto('/');
  const rowAfter = page.getByTestId(`project-${pid}`);
  await expect(rowAfter).toBeVisible();
  await expect(rowAfter.getByTestId(`project-${pid}-pending`)).toBeVisible();
  await expect(rowAfter.getByTestId(`project-${pid}-pending`)).toHaveText('2');
});

test('picker rows sort by recency, most-recently-modified first', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-picker-recency'));

  await page.goto('/');
  const list = page.getByTestId('project-list');
  await expect(list).toBeVisible();

  const ourRow = page.getByTestId(`project-${pid}`);
  const seededRow = list.getByText('Local stories');
  await expect(ourRow).toBeVisible();
  await expect(seededRow).toBeVisible();

  const rows = list.locator('.home-card');
  const ourIndex = await rows.evaluateAll(
    (els, targetPid) => els.findIndex((el) => el.getAttribute('data-testid') === `project-${targetPid}`),
    pid,
  );
  const seededIndex = await rows.evaluateAll((els) =>
    els.findIndex((el) => el.textContent?.includes('Local stories')),
  );
  expect(ourIndex).toBeGreaterThanOrEqual(0);
  expect(seededIndex).toBeGreaterThanOrEqual(0);
  expect(ourIndex).toBeLessThan(seededIndex);
});
