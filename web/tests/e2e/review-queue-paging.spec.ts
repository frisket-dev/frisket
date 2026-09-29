import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openProject, textPdf, uniqueName } from './helpers';

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

async function mockRunListing(page: Page, pid: string, sheetId: number, total: number, pending: () => number) {
  await page.route(`**/api/projects/${pid}/review/runs?**`, async (route) => {
    const counts = { eligible_count: total, reviewed_count: total - pending(), accepted_count: total - pending(),
      incorrect_count: 0, unreviewed_count: pending(), confidence_count: total };
    await route.fulfill({ json: {
      schema_version: 'frisket.review_runs_page.v1', offset: 0, limit: 50, total: 1, has_more: false, next_offset: null,
      runs: [{ run_id: 901, sheet_id: sheetId, sheet_name: 'stories', action_kind: 'map.classify', action_name: 'Classify',
        model: 'test-model', started_at: '2026-09-29T00:00:00Z', review_status: 'open', review_completed_at: null,
        total: counts, fields: [{ ...counts, column_id: 2, column_name: 'beat', column_type: 'category' }] }],
    } });
  });
}

test('review overlay pages bundles and refreshes global count after decisions', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-review-paging'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'story\n"Story 1"\n');
  const total = 51;
  let remainingRows = Array.from({ length: total }, (_value, index) => index + 1);
  let reviewCount = remainingRows.length;
  await mockRunListing(page, pid, sheetId, total, () => reviewCount);
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
  await expect(reviewButton).toBeEnabled();
  expect(bundleRequests).toEqual([]);

  await page.getByTestId('review-queue-button').click();
  const queue = page.getByTestId('review-queue');
  await expect(queue).toBeVisible();
  await expect.poll(() => bundleRequests.some((query) => new URLSearchParams(query).get('offset') === '0')).toBe(true);
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
  await expect.poll(() => bundleRequests.some((query) => new URLSearchParams(query).get('offset') === '25')).toBe(true);
  await expect(queue.getByTestId('review-page-status')).toContainText('Showing 26-50 of 51 bundles');
  await expect(sourcePanel).toContainText('Story 26');

  await queue.getByTestId('review-page-older').click();
  await expect.poll(() => bundleRequests.some((query) => new URLSearchParams(query).get('offset') === '50')).toBe(true);
  await expect(queue.getByTestId('review-page-status')).toContainText('Showing 51-51 of 51 bundles');
  await expect(sourcePanel).toContainText('Story 51');

  const previousPageRequests = bundleRequests.filter((query) => new URLSearchParams(query).get('offset') === '25').length;
  await page.getByTestId('review-accept').click();
  await expect.poll(() => decisions.length).toBe(1);
  await expect.poll(() => bundleRequests.filter((query) => new URLSearchParams(query).get('offset') === '25').length)
    .toBeGreaterThan(previousPageRequests);
  await expect(queue.getByTestId('review-page-status')).toContainText('Showing 26-50 of 50 bundles');
  await expect(reviewButton.locator('.badge')).toHaveText(String(total - 1));

  await queue.getByTestId('review-page-newer').click();
  await expect.poll(() => bundleRequests.filter((query) => new URLSearchParams(query).get('offset') === '0').length)
    .toBeGreaterThan(1);
  await expect(queue.getByTestId('review-page-status')).toContainText('Showing 1-25 of 50 bundles');
  expect(sheetFanoutRequests).toEqual([]);
  expect(decisions[0]).toMatchObject({
    action_id: 'review.decision',
    scope: { kind: 'project' },
    params: {
      decision: 'accept',
      run_id: 901,
      row_id: 51,
      column_id: 2,
    },
  });
});

test('review keeps a same-row PDF and long proposed values inside a bounded, scrollable surface', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const pid = await createProject(page.request, uniqueName('e2e-review-layout'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'story\n"Story 1"\n');
  await mockRunListing(page, pid, sheetId, 1, () => 1);
  const pdfHash = 'a'.repeat(64);
  const longValue = Array.from({ length: 160 }, () => 'A deliberately long reviewed output remains scrollable.').join(' ');

  await page.route(`**/api/projects/${pid}/review/count`, async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ count: 1 }) });
  });
  await page.route(`**/api/projects/${pid}/review/bundles**`, async (route) => {
    const body = pageBody(0, 25, [1], sheetId);
    body.bundles[0].source = {
      story: 'Story 1',
      filing: JSON.stringify({ blob: pdfHash, filename: 'filing.pdf', mime: 'application/pdf' }),
    };
    body.bundles[0].fields[0].value = longValue;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.route(`**/api/projects/${pid}/cells/1/2/evidence`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.cell_evidence.v1',
        sheet_id: sheetId,
        row_id: 1,
        column_id: 2,
        current_value_ref: { kind: 'run_result', run_id: 901 },
        links: [],
        stale_count: 0,
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/blobs/${pdfHash}`, async (route) => {
    // The reader receives a real PDF from the same project blob route; the
    // review surface never treats a filename-shaped string as a URL.
    await route.fulfill({ status: 200, contentType: 'application/pdf', body: textPdf([['Review source']]) });
  });

  await openProject(page, pid, sheetId);
  await page.getByTestId('review-queue-button').click();
  const queue = page.getByTestId('review-queue');
  const sourcePdf = queue.getByTestId('review-source-pdf-source');
  const output = queue.getByTestId('review-output-panel');
  const fields = queue.getByTestId('review-bundle-fields');
  await expect(sourcePdf).toBeVisible();
  await expect(sourcePdf.locator('canvas')).toBeVisible();
  await expect(output).toBeVisible();

  const [queueBox, sourceBox, outputBox, actionsBox] = await Promise.all([
    queue.boundingBox(),
    sourcePdf.boundingBox(),
    output.boundingBox(),
    queue.locator('.review-actions').boundingBox(),
  ]);
  expect(queueBox).not.toBeNull();
  expect(sourceBox).not.toBeNull();
  expect(outputBox).not.toBeNull();
  expect(actionsBox).not.toBeNull();
  expect(queueBox!.width).toBe(1280);
  expect(queueBox!.height).toBe(800);
  expect(sourceBox!.x + sourceBox!.width).toBeLessThanOrEqual(queueBox!.x + queueBox!.width);
  expect(outputBox!.x + outputBox!.width).toBeLessThanOrEqual(queueBox!.x + queueBox!.width);
  expect(actionsBox!.y + actionsBox!.height).toBeLessThanOrEqual(outputBox!.y + outputBox!.height);
  expect(await fields.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('review-1280x800.png') });

  await page.setViewportSize({ width: 390, height: 650 });
  await expect(sourcePdf).toBeVisible();
  const [narrowQueue, narrowSource, narrowOutput, narrowActions] = await Promise.all([
    queue.boundingBox(),
    sourcePdf.boundingBox(),
    output.boundingBox(),
    queue.locator('.review-actions').boundingBox(),
  ]);
  expect(narrowQueue).not.toBeNull();
  expect(narrowSource).not.toBeNull();
  expect(narrowOutput).not.toBeNull();
  expect(narrowActions).not.toBeNull();
  expect(narrowSource!.x + narrowSource!.width).toBeLessThanOrEqual(narrowQueue!.x + narrowQueue!.width);
  expect(narrowOutput!.x + narrowOutput!.width).toBeLessThanOrEqual(narrowQueue!.x + narrowQueue!.width);
  expect(narrowActions!.y + narrowActions!.height).toBeLessThanOrEqual(narrowOutput!.y + narrowOutput!.height);
  await page.screenshot({ path: testInfo.outputPath('review-390x650.png') });
});
