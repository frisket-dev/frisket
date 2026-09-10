import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';

type WireReviewBundle = {
  id: string;
  run_id: number;
  row_id: number;
  sheet_id: number;
  sheet_name: string;
  action_kind: string;
  action_name: string;
  model: string;
  confidence: number;
  source: Record<string, string>;
  fields: Array<Record<string, unknown>>;
  evidence: Array<Record<string, unknown>>;
};

function reviewBundle(rowId: number, sheetId: number): WireReviewBundle {
  return {
    id: `901:${rowId}`,
    run_id: 901,
    row_id: rowId,
    sheet_id: sheetId,
    sheet_name: 'stories',
    action_kind: 'map.classify',
    action_name: 'Classify rows',
    model: 'gemini/gemini-2.5-flash',
    confidence: 0.2 + rowId / 1000,
    source: { story: `Story ${rowId}` },
    fields: [
      {
        run_id: 901,
        row_id: rowId,
        column_id: 2,
        column_name: 'risk',
        column_type: 'category',
        sheet_id: sheetId,
        value: rowId % 2 ? 'high' : 'low',
        confidence: 0.2 + rowId / 1000,
        justification: `risk ${rowId}`,
        review_state: 'unreviewed',
        role: 'field',
        chore: true,
      },
    ],
    evidence: [
      {
        run_id: 901,
        row_id: rowId,
        column_id: 3,
        column_name: 'source_note',
        column_type: 'text',
        sheet_id: sheetId,
        value: `supporting note ${rowId}`,
        confidence: 0.2 + rowId / 1000,
        justification: '',
        review_state: 'unreviewed',
        role: 'evidence',
        chore: false,
      },
    ],
  };
}

function pageBody(offset: number, limit: number, rowIds: number[], sheetId: number) {
  const end = Math.min(offset + limit, rowIds.length);
  const visibleRows = rowIds.slice(offset, end);
  return {
    schema_version: 'frisket.review_bundles_page.v1',
    offset,
    limit,
    total: rowIds.length,
    has_more: end < rowIds.length,
    next_offset: end < rowIds.length ? end : null,
    bundles: visibleRows.map((rowId) => reviewBundle(rowId, sheetId)),
  };
}

test('review overlay pages bundles and refreshes global count after decisions', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-review-paging'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'story\n"Story 1"\n');
  const total = 51;
  let remainingRows = Array.from({ length: total }, (_value, index) => index + 1);
  let reviewCount = remainingRows.length;
  const bundleRequests: string[] = [];
  const decisions: Array<Record<string, unknown>> = [];
  const sheetFanoutRequests: string[] = [];

  await page.route(`**/api/projects/${pid}/review/count`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ count: reviewCount }),
    });
  });
  await page.route(`**/api/projects/${pid}/review/bundles**`, async (route) => {
    const url = new URL(route.request().url());
    const offsetParam = url.searchParams.get('offset');
    const limitParam = url.searchParams.get('limit');
    if (offsetParam === null || limitParam === null) {
      await route.fulfill({
        status: 599,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'unpaged review bundles are blocked' }),
      });
      return;
    }
    const offset = Number(offsetParam);
    const limit = Number(limitParam);
    bundleRequests.push(url.searchParams.toString());
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(pageBody(offset, limit, remainingRows, sheetId)),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload.action_id === 'review.decision') {
      decisions.push(payload);
      const params = payload.params as { row_id?: number } | undefined;
      const rowId = Number(params?.row_id);
      if (Number.isInteger(rowId)) {
        remainingRows = remainingRows.filter((candidate) => candidate !== rowId);
      }
      reviewCount = remainingRows.length;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          action_id: 'review-paging-decision',
          kind: 'review.decision',
          status: 'completed',
          outputs: [],
          errors: [],
        }),
      });
      return;
    }
    await route.continue();
  });

  await openProject(page, pid, sheetId);
  await page.route(new RegExp(`/api/projects/${pid}/sheets(\\?|$)`), async (route) => {
    sheetFanoutRequests.push(route.request().url());
    await route.fulfill({
      status: 599,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'review overlay must not fan out through sheets' }),
    });
  });
  const reviewButton = page.getByTestId('review-queue-button');
  await expect(reviewButton.locator('.badge')).toHaveText(String(total));
  expect(bundleRequests).toEqual([]);

  await reviewButton.click();
  const queue = page.getByTestId('review-queue');
  await expect(queue).toBeVisible();
  await expect.poll(() => bundleRequests).toContain('offset=0&limit=25');
  await expect(queue.getByTestId('review-page-status')).toContainText('Showing 1-25 of 51 bundles');
  const sourcePanel = queue.getByTestId('review-source-panel');
  const outputPanel = queue.getByTestId('review-output-panel');
  await expect(sourcePanel).toBeVisible();
  await expect(outputPanel).toBeVisible();
  await expect(sourcePanel).toContainText('Story 1');
  await expect(sourcePanel).toContainText('supporting note 1');
  await expect(outputPanel).toContainText('risk');
  await expect(outputPanel).toContainText('high');
  await expect(outputPanel).not.toContainText('Story 1');
  await expect(outputPanel).not.toContainText('supporting note 1');
  await expect(sourcePanel).not.toContainText('risk');
  const sourceBox = await sourcePanel.boundingBox();
  const outputBox = await outputPanel.boundingBox();
  expect(sourceBox).not.toBeNull();
  expect(outputBox).not.toBeNull();
  expect(sourceBox!.x + sourceBox!.width).toBeLessThan(outputBox!.x);

  await queue.getByTestId('review-page-older').click();
  await expect.poll(() => bundleRequests).toContain('offset=25&limit=25');
  await expect(queue.getByTestId('review-page-status')).toContainText('Showing 26-50 of 51 bundles');
  await expect(sourcePanel).toContainText('Story 26');

  await queue.getByTestId('review-page-older').click();
  await expect.poll(() => bundleRequests).toContain('offset=50&limit=25');
  await expect(queue.getByTestId('review-page-status')).toContainText('Showing 51-51 of 51 bundles');
  await expect(sourcePanel).toContainText('Story 51');

  const previousPageRequests = bundleRequests.filter((query) => query === 'offset=25&limit=25').length;
  await page.getByTestId('review-accept').click();
  await expect.poll(() => decisions.length).toBe(1);
  await expect.poll(() => bundleRequests.filter((query) => query === 'offset=25&limit=25').length)
    .toBeGreaterThan(previousPageRequests);
  await expect(queue.getByTestId('review-page-status')).toContainText('Showing 26-50 of 50 bundles');
  await expect(reviewButton.locator('.badge')).toHaveText(String(total - 1));

  await queue.getByTestId('review-page-newer').click();
  await expect.poll(() => bundleRequests.filter((query) => query === 'offset=0&limit=25').length)
    .toBeGreaterThan(1);
  await expect(queue.getByTestId('review-page-status')).toContainText('Showing 1-25 of 50 bundles');
  expect(sheetFanoutRequests).toEqual([]);
  expect(decisions[0]).toMatchObject({
    schema_version: 'frisket.action.v2',
    kind: 'review.decision',
    capabilities: ['project:write'],
    params: {
      decision: 'accept',
      target: {
        kind: 'result_cell',
      },
    },
  });
});
