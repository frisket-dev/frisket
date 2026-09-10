// Review queue: every AI result starts 'unreviewed'; the overlay triages one
// result at a time with a/r keyboard shortcuts. Own project (mutating).
// The seeding run is deterministic; run UI and live model behavior are covered
// by run.spec.ts and the golden/live specs.

import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';
import { seedReviewClassifyRun } from './reviewFixtures';

const CSV = `snippet
"The zoning board approved a variance for the mayor's cousin without discussion."
"Bus ridership rose 4 percent after the new crosstown route opened."
"A broken water main flooded two blocks of Elm Street on Tuesday."
`;

test('review queue: a/r resolve items and the count decrements', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-review'));
  const sheetId = await importCsv(page.request, pid, 'review.csv', CSV);
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

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });

  // Status-bar badge shows the pending count; clicking opens the overlay.
  const reviewButton = page.getByTestId('review-queue-button');
  await expect(reviewButton.locator('.badge')).toBeVisible();
  const initial = Number(await reviewButton.locator('.badge').innerText());
  expect(initial).toBeGreaterThanOrEqual(2);

  await reviewButton.click();
  const queue = page.getByTestId('review-queue');
  await expect(queue).toBeVisible();
  const contribution = page.getByTestId('workbench-contribution-frisket-core-view-review-queue');
  await expect(contribution).toBeVisible();
  await expect(contribution).toHaveAttribute('data-schema-version', 'frisket.workbench.view.v1');
  await expect(contribution).toHaveAttribute('data-contribution-id', 'frisket.core.view.review_queue');
  await expect(contribution).toHaveAttribute('data-host', 'modalOrPeek');
  await expect(contribution).toHaveAttribute('data-mode', 'peek');
  await expect(contribution).toHaveAttribute(
    'data-runtime-component-key',
    'core.views.ReviewQueue',
  );
  const reviewCapabilities = (await contribution.getAttribute('data-required-capabilities'))
    ?.split(' ') ?? [];
  expect(reviewCapabilities).toEqual(expect.arrayContaining([
    'review.queue.list',
    'review.decision',
    'evidence.open',
    'grid.refresh',
  ]));
  await expect(queue).toContainText(`${initial} pending`);
  await expect(page.getByTestId('review-card')).toBeVisible();

  // a = accept, r = reject; each resolves the current item.
  await page.keyboard.press('a');
  await expect(queue).toContainText(`${initial - 1} pending`);
  await page.keyboard.press('r');
  await expect(queue).toContainText(`${initial - 2} pending`);

  // Esc closes the overlay route; the badge reflects
  // the new count.
  await page.keyboard.press('Escape');
  await expect(queue).toBeHidden();
  await expect(reviewButton.locator('.badge')).toHaveText(String(initial - 2));
});
