import { expect, test, type Page } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  importCsv,
  openAction,
  revealRibbonAction,
  sheetColumns,
  uniqueName,
} from './helpers';

type PostedFetchUrlAction = {
  action_id?: string;
  scope?: { kind: string; sheet_id: number; row_ids?: number[] };
  params?: { source?: string };
  output_names?: { media?: string };
  idempotency_key?: string;
};

async function mockV1FetchUrlRun(page: Page, pid: string): Promise<PostedFetchUrlAction[]> {
  const posts: PostedFetchUrlAction[] = [];
  await page.route(`**/api/projects/${pid}/run`, async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'fetch_url should not use legacy /run' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as PostedFetchUrlAction;
    posts.push(body);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: body.action_id, action_id: 'e2e-fetch-url' },
        status: 'completed',
        project_id: pid,
        run_id: null,
        op_ids: [884],
        outputs: [],
        receipt_id: 'receipt-web-fetch-url',
        warnings: [],
        errors: [],
      }),
    });
  });
  return posts;
}

test('link or text URL column launches media.fetch_url with one source and output name', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-url-column-download'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'urls.csv',
    'url,note\n"https://example.com/files/report.pdf","direct download"\n',
  );
  const posts = await mockV1FetchUrlRun(page, pid);

  const catalogLoaded = page.waitForResponse((response) => (
    response.url().endsWith('/actions/v1/catalog') && response.status() === 200
  ));
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await catalogLoaded;

  // The retired discovery category listing is replaced by the Act ribbon.
  // Assert the served-catalog action is reachable without freezing its tab.
  await expect(await revealRibbonAction(page, 'media.fetch_url')).toContainText('Fetch URL');
  await openAction(page, 'media.fetch_url');
  await expect(page.getByTestId('generated-action-form')).toBeVisible();
  await expect(page.getByTestId('field-source')).toHaveValue('url');
  await expect(page.getByTestId('field-output-media')).toHaveValue('media');

  await page.getByTestId('field-output-media').fill('downloaded_media');
  await page.getByTestId('generated-action-run').click();

  await expect.poll(() => posts.length, { timeout: 5000 }).toBe(1);
  const posted = posts[0];
  expect(posted.action_id).toBe('media.fetch_url');
  expect(posted.scope?.sheet_id).toBe(sheetId);
  expect(posted.params).toEqual({ source: 'url' });
  expect(posted.output_names?.media).toBe('downloaded_media');
  expect(posted.scope?.row_ids).toBeUndefined();
  await expect(page.getByTestId('error-toast')).toHaveCount(0);
});

test('URL column header menu shortcut runs media.fetch_url for that column', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-url-column-header-fetch'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'urls.csv',
    'enclosure_url,note\n"https://example.com/files/episode.mp3","podcast"\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const posts = await mockV1FetchUrlRun(page, pid);

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await clickHeaderMenu(page, columns, 'enclosure_url');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();

  // Configure-first (workbench-ia-action-drawer-v1): the column action opens the
  // drawer pre-bound to `enclosure_url`; running it posts the same
  // media.fetch_url v1 spec the retired header fast-path used to post directly.
  await page.getByTestId('header-menu-action-media.fetch_url').click();
  await expect(page.getByTestId('generated-action-form')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('field-output-media')).toHaveValue('media');
  await page.getByTestId('generated-action-run').click();

  await expect.poll(() => posts.length, { timeout: 5000 }).toBe(1);
  const posted = posts[0];
  expect(posted.action_id).toBe('media.fetch_url');
  expect(posted.scope?.sheet_id).toBe(sheetId);
  expect(posted.params).toEqual({ source: 'enclosure_url' });
  expect(posted.output_names?.media).toBe('media');
  expect(posted.scope?.row_ids).toBeUndefined();
  await expect(page.getByTestId('error-toast')).toHaveCount(0);
});
