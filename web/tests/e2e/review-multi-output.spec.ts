import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openProject,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';
import { seedReviewClassifyRun } from './reviewFixtures';

const CSV = `story
"Mayor met a lobbyist before the zoning vote."
`;

test('review queue groups sibling outputs and resolves fields independently', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-review-multi-output'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', CSV);
  seedReviewClassifyRun({
    pid,
    sheetId,
    sourceColumns: ['story'],
    context: 'Classify the local news snippet. Use beat=civic, tone=high, and confidence 0.41.',
    fields: [
      {
        name: 'beat',
        type: 'category',
        labels: ['civic', 'private'],
        description: 'best-fit public affairs beat',
      },
      {
        name: 'tone',
        type: 'category',
        labels: ['low', 'high'],
        description: 'news urgency',
      },
    ],
    reply: {
      beat: 'civic',
      tone: 'high',
      beat_justification: 'mentions city government',
      tone_justification: 'the event affects public decisions',
      beat_confidence: 0.41,
    },
  });

  const bundleResponse = await page.request.get(`/api/projects/${pid}/review/bundles`);
  expect(bundleResponse.ok()).toBeTruthy();
  const bundlesPage = await bundleResponse.json();
  const bundles = bundlesPage.bundles;
  expect(bundles).toHaveLength(1);
  expect(bundles[0].fields.map((field: { column_name: string }) => field.column_name).sort()).toEqual([
    'beat',
    'tone',
  ]);

  const reviewDecisions: Array<Record<string, unknown>> = [];
  let legacyReviewItemCalled = false;
  await page.route(`**/api/projects/${pid}/review/item`, async (route) => {
    legacyReviewItemCalled = true;
    await route.fulfill({
      status: 599,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'legacy review item route is blocked in v1 proof' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload?.action_id === 'review.decision') reviewDecisions.push(payload);
    await route.continue();
  });

  await openProject(page, pid, sheetId);
  const reviewButton = page.getByTestId('review-queue-button');
  await expect(reviewButton.locator('.badge')).toHaveText('2');
  await expect(reviewButton).toBeEnabled();

  await page.getByTestId('review-queue-button').click();
  const queue = page.getByTestId('review-queue');
  await expect(queue).toContainText('0 of 2 decided');
  await expect(queue.getByTestId('review-page-status')).toHaveText('1 / 1');

  const beat = page.getByTestId('review-field-beat');
  const tone = page.getByTestId('review-field-tone');
  await expect(beat).toBeVisible();
  await expect(tone).toBeVisible();
  await queue.getByRole('button', { name: /^Row fields/ }).click();
  await expect(queue.getByTestId('review-row-fields-drawer')).toContainText('Mayor met a lobbyist');
  await queue.getByRole('button', { name: 'Close row fields' }).click();

  await queue.getByRole('button', { name: 'Select beat review field' }).click();
  await page.getByTestId('review-edit').click();
  await page.getByTestId('review-edit-input').fill('accountability');
  await queue.getByRole('button', { name: 'Save edit' }).click();

  await expect(beat).toHaveAttribute('data-review-state', 'verified');
  await expect(beat).toHaveAttribute('data-review-changed', 'true');
  await expect(beat).toContainText('Corrected');
  await expect(tone).toHaveAttribute('data-review-state', 'unreviewed');
  await expect(queue).toContainText('1 of 2 decided');

  expect(legacyReviewItemCalled).toBe(false);
  expect(reviewDecisions).toHaveLength(1);
  expect(reviewDecisions[0]).toMatchObject({
    action_id: 'review.decision',
    scope: { kind: 'project' },
    params: {
      decision: 'edit',
      value: 'accountability',
    },
  });
  expect(String(reviewDecisions[0].idempotency_key)).toContain('web-review.decision:');

  await queue.getByRole('button', { name: 'Select tone review field' }).click();
  const note = page.getByTestId('review-note-input');
  await note.fill('The source says low urgency.');
  // Queue shortcuts must not resolve a field while the reviewer is writing.
  await note.press('r');
  expect(reviewDecisions).toHaveLength(1);
  // It remains ordinary note text while typing; restore the intended note
  // before asserting the persisted payload.
  await note.press('Backspace');
  await note.blur();
  await expect(note).toBeEnabled();
  await queue.getByRole('button', { name: 'Reject tone', exact: true }).click();
  await expect(page.getByText('Row done', { exact: true })).toBeVisible();

  expect(legacyReviewItemCalled).toBe(false);
  expect(reviewDecisions).toHaveLength(2);
  expect(reviewDecisions[1]).toMatchObject({
    action_id: 'review.decision',
    scope: { kind: 'project' },
    params: {
      decision: 'reject',
    },
  });

  const noted = await (await page.request.get(`/api/projects/${pid}/review/bundles?include_reviewed=true`)).json();
  expect(noted.bundles[0].review_note).toBe('The source says low urgency.');
  const finalBundlesPage = await (await page.request.get(`/api/projects/${pid}/review/bundles`)).json();
  const finalBundles = finalBundlesPage.bundles;
  expect(finalBundles).toHaveLength(0);
  const columns = await sheetColumns(page.request, pid, sheetId);
  const data = await sheetData(page.request, pid, sheetId);
  const beatColumn = columns.find((column) => column.name === 'beat');
  const toneColumn = columns.find((column) => column.name === 'tone');
  expect(beatColumn).toBeTruthy();
  expect(toneColumn).toBeTruthy();
  expect(data.rows[0].cells[String(beatColumn!.id)]).toBe('accountability');
  // Verdict-only Reject keeps the generated value visible; clearing is the
  // separate, explicitly labelled Shift+R action.
  expect(data.rows[0].cells[String(toneColumn!.id)]).toBe('high');
});

test('Shift+R rejects and clears a focused result', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-review-clear'));
  const sheetId = await importCsv(page.request, pid, 'review-clear.csv', CSV);
  seedReviewClassifyRun({
    pid,
    sheetId,
    sourceColumns: ['story'],
    context: 'Classify this story.',
    fields: [{ name: 'beat', type: 'category', labels: ['civic'], description: 'beat' }],
    reply: { beat: 'civic', beat_confidence: 0.41 },
  });

  const decisions: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload?.action_id === 'review.decision') decisions.push(payload);
    await route.continue();
  });
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('review-queue-button')).toBeEnabled();
  await page.getByTestId('review-queue-button').click();
  const clear = page.getByTestId('review-reject-clear');
  await expect(clear).toHaveAccessibleName('Reject and clear');
  await page.keyboard.press('Shift+R');
  await expect(page.getByText('Row done', { exact: true })).toBeVisible();

  expect(decisions).toHaveLength(1);
  expect(decisions[0]).toMatchObject({
    action_id: 'review.decision',
    params: { decision: 'reject_clear' },
  });
  const columns = await sheetColumns(page.request, pid, sheetId);
  const data = await sheetData(page.request, pid, sheetId);
  const beatColumn = columns.find((column) => column.name === 'beat');
  expect(data.rows[0].cells[String(beatColumn!.id)]).toBeNull();
});
