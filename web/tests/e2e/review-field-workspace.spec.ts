import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';
import { seedReviewClassifyRun } from './reviewFixtures';
import { extractRecordedContent } from './textCitationFixtures';

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
  await queue.getByRole('button', { name: 'Accept all', exact: true }).click();
  await expect(queue.getByTestId('review-page-status')).toHaveText('2 / 2');
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
    const screenshots = path.resolve(process.env.FRISKET_REVIEW_SCREENSHOTS!);
    mkdirSync(screenshots, { recursive: true });
    await page.screenshot({
      path: path.join(screenshots, 'review-workspace@2x.png'),
      animations: 'disabled',
    });
  }
});


test('review uses cited-source tabs and the canonical text highlighter without nested scrolling', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('review-cited-source'));
  const seeded = extractRecordedContent(pid);
  const citationRequests: string[] = [];
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.includes('/evidence')) citationRequests.push(request.url());
  });
  await openProject(page, pid, seeded.sheetId);
  await page.getByTestId('review-queue-button').click();
  const queue = page.getByTestId('review-queue');
  await expect(queue.getByRole('tab')).toHaveCount(2);
  await queue.getByRole('tab', { name: /filing_text/ }).click();
  const source = queue.getByTestId('evidence-text-body');
  await expect(source).toContainText('EMPLOYMENT DISCRIMINATION COMPLAINT');
  await expect(queue.getByTestId('evidence-text-highlight').first()).toBeVisible();
  const loadedRequests = citationRequests.length;
  expect(loadedRequests).toBeGreaterThan(0);
  const sourceElement = await source.elementHandle();
  await queue.getByRole('button', { name: /^Accept / }).first().click();
  await expect(queue.getByTestId('review-bundle-fields').locator('[data-review-state="verified"]').first()).toBeVisible();
  expect(citationRequests).toHaveLength(loadedRequests);
  expect(await sourceElement!.evaluate((element) => element.isConnected)).toBe(true);
  expect(await source.evaluate((element) => getComputedStyle(element).maxHeight)).toBe('none');
  expect(await source.evaluate((element) => getComputedStyle(element).overflowY)).toBe('visible');
  const card = await queue.getByTestId('review-card').boundingBox();
  expect(card!.height).toBeGreaterThan(800);
  if (process.env.FRISKET_REVIEW_SCREENSHOTS) {
    mkdirSync(process.env.FRISKET_REVIEW_SCREENSHOTS, { recursive: true });
    await page.screenshot({ path: path.join(process.env.FRISKET_REVIEW_SCREENSHOTS, 'review-cited-sources@2x.png') });
  }
  await page.setViewportSize({ width: 620, height: 850 });
  await expect(queue.getByTestId('review-note-input')).toBeVisible();
  expect(await queue.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  if (process.env.FRISKET_REVIEW_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.FRISKET_REVIEW_SCREENSHOTS, 'review-narrow@2x.png') });
});
