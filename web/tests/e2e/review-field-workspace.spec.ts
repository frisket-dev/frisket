import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';
import { seedReviewClassifyRun } from './reviewFixtures';

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });

test('review workspace saves independent decisions, resets verdicts, and keeps a row note after leaving and reopening', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-review-workspace'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'story\n"First filing"\n"Second filing"\n');
  seedReviewClassifyRun({
    pid, sheetId, sourceColumns: ['story'], context: 'Classify the filing.',
    fields: [
      { name: 'beat', type: 'category', labels: ['civic', 'private'] },
      { name: 'tone', type: 'category', labels: ['low', 'high'] },
    ],
    reply: { beat: 'civic', tone: 'high' },
  });

  await openProject(page, pid, sheetId);
  await page.getByTestId('review-queue-button').click();
  const queue = page.getByTestId('review-queue');
  const beat = queue.getByTestId('review-field-beat');
  const tone = queue.getByTestId('review-field-tone');
  await expect(beat).toBeVisible();
  await expect(tone).toBeVisible();

  await queue.getByRole('button', { name: 'Accept beat', exact: true }).click();
  await expect(beat).toHaveAttribute('data-review-state', 'verified');
  await queue.getByRole('button', { name: 'Reject tone', exact: true }).click();
  await expect(tone).toHaveAttribute('data-review-state', 'rejected');
  await queue.getByRole('button', { name: 'Reset', exact: true }).click();
  await expect(beat).toHaveAttribute('data-review-state', 'unreviewed');
  await expect(tone).toHaveAttribute('data-review-state', 'unreviewed');
  await expect(beat).toContainText('civic');
  await expect(tone).toContainText('high');

  const noteText = 'The first filing needs source verification.';
  const note = queue.getByTestId('review-note-input');
  await note.fill(noteText);
  await queue.locator('[aria-label="Next row"]').click();
  await expect(queue.getByTestId('review-page-status')).toContainText('2');
  const runId = await queue.getByTestId('review-run-select').inputValue();
  const afterLeave = await page.request.get(`/api/projects/${pid}/review/bundles?run_id=${runId}&include_reviewed=true`);
  expect(afterLeave.ok()).toBeTruthy();
  expect((await afterLeave.json()).bundles.some((bundle: { review_note: string | null }) => bundle.review_note === noteText)).toBeTruthy();

  await queue.getByRole('button', { name: 'Close review queue' }).click();
  await expect(queue).toBeHidden();
  await page.getByTestId('review-queue-button').click();
  const reopenedNote = queue.getByTestId('review-note-input');
  await expect(reopenedNote).toBeVisible();
  if (await reopenedNote.inputValue() !== noteText) {
    const forward = queue.locator('[aria-label="Next row"]');
    const backward = queue.locator('[aria-label="Previous row"]');
    if (await forward.isEnabled()) await forward.click();
    else await backward.click();
  }
  await expect(reopenedNote).toHaveValue(noteText);

  if (process.env.FRISKET_REVIEW_SCREENSHOTS) {
    const screenshots = path.resolve(process.cwd(), 'screenshots');
    mkdirSync(screenshots, { recursive: true });
    await page.screenshot({
      path: path.join(screenshots, 'review-workspace@2x.png'),
      animations: 'disabled',
    });
  }
});
