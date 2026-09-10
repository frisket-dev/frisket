import { expect, test } from '@playwright/test';
import {
  createProject,
  dblclickCell,
  importCsv,
  openAction,
  openProject,
  selectRow,
  sheetColumns,
  uniqueName,
} from './helpers';
import {
  closeActionFormIfOpen,
  seedSourceCellEvidence,
  stubV1ActionRun,
} from './investigativeActionFixtures';

test('URL capture launches web.capture_page with render and WARC options', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-url-capture'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'urls.csv',
    [
      'url,topic',
      '"https://example.test/minutes","board minutes"',
      '"https://example.test/contracts","contracts"',
    ].join('\n'),
  );
  const seeded = seedSourceCellEvidence({
    pid,
    sheetId,
    columnName: 'url',
    producerKind: 'web.capture_page',
  });
  const columns = await sheetColumns(page.request, pid, sheetId);
  const posts = await stubV1ActionRun({ page, pid, runId: 9831 });

  await openProject(page, pid, sheetId);
  await selectRow(page, 0);
  await openAction(page, 'web.capture_page');
  await expect(page.getByTestId('field-source')).toHaveValue('url');
  await page.getByTestId('field-output-page').fill('page');
  await page.getByTestId('field-render_mode').selectOption('playwright');
  await page.getByTestId('page-capture-advanced').locator('summary').click();
  await page.getByTestId('field-include_warc').check();
  await expect(page.getByTestId('field-max_bytes-unit')).toHaveValue('MB');
  await page.getByTestId('field-max_bytes').fill('7');
  await page.getByTestId('field-timeout_ms').fill('45');
  await page.getByTestId('generated-action-run').click();

  await expect.poll(() => posts.v1Posts.length, { timeout: 5000 }).toBe(1);
  expect(posts.legacyPosts).toHaveLength(0);
  const posted = posts.v1Posts[0];
  expect(posted.action_id).toBe('web.capture_page');
  expect(posted).not.toHaveProperty('capabilities');
  expect(posted.scope).toEqual({ kind: 'sheet_rows', sheet_id: Number(sheetId), row_ids: [expect.any(Number)] });
  expect(posted.output_names).toEqual({ page: 'page' });
  const params = posted.params as Record<string, unknown>;
  expect(params).toMatchObject({
    source: 'url',
    render_mode: 'playwright',
    include_warc: true,
    max_bytes: 7000000,
    timeout_ms: 45000,
  });

  await closeActionFormIfOpen(page);
  await dblclickCell(page, columns, 'url', 0);
  const rowDrawer = page.getByTestId('row-drawer');
  await expect(rowDrawer).toBeVisible();
  await expect(rowDrawer.getByTestId('cell-evidence-active-url')).toBeVisible();
  await rowDrawer.getByTestId('cell-evidence-open-url').click();
  const viewer = page.getByTestId('evidence-viewer');
  await expect(viewer).toBeVisible();
  await expect(viewer.getByTestId('evidence-export-ref')).toContainText(seeded.stableId);
});
