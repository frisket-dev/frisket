import { expect, type Page } from '@playwright/test';
import { captureCheckpoint, type CaptureRecord } from './capture';

/** Capture actual saved results and one checked decision, without generating another model response. */
export async function captureReview(page: Page, stage: string, runId: number): Promise<CaptureRecord[]> {
  const captures: CaptureRecord[] = [];
  const entry = page.getByTestId('review-queue-button');
  await expect(entry).toBeEnabled();
  captures.push(await captureCheckpoint(page, { id: 'review-entry', root: entry, annotations: [] }, stage));
  await entry.click();
  const queue = page.getByTestId('review-queue');
  await expect(queue).toBeVisible();
  await queue.getByTestId('review-run-select').selectOption(String(runId));
  const field = queue.getByTestId('review-field-case_number_2');
  await expect(field).toBeVisible({ timeout: 30_000 });
  await queue.getByRole('button', { name: 'Select case_number_2 review field' }).click();
  const source = queue.locator('[data-testid="review-citation-preview"], [data-testid="review-input-preview"]');
  await expect(source).toBeVisible();
  await expect(source.getByText('Loading cited sources…')).toHaveCount(0, { timeout: 30_000 });
  captures.push(await captureCheckpoint(page, { id: 'review-queue', root: queue, annotations: [] }, stage));
  captures.push(await captureCheckpoint(page, { id: 'review-source', root: source, maxHeight: 380, annotations: [] }, stage));
  await queue.getByRole('button', { name: 'Accept case_number_2', exact: true }).click();
  await expect(field).toHaveAttribute('data-review-state', 'verified');
  await queue.getByRole('button', { name: 'Results', exact: true }).click();
  const results = page.getByTestId('review-results');
  await expect(results).toContainText('1 accepted');
  captures.push(await captureCheckpoint(page, { id: 'review-results', root: results, annotations: [] }, stage));
  await results.getByRole('button', { name: 'Close review results' }).click();
  await queue.getByRole('button', { name: 'Close review queue' }).click();
  return captures;
}
