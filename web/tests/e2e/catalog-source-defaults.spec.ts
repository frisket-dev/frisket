import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

type V1ActionSpec = {
  action_id?: string;
  scope?: { kind: string; sheet_id: number };
  output_names?: Record<string, string>;
  params?: Record<string, unknown>;
  idempotency_key?: string;
};

async function patchToMarkdownSourceRequirement(page: Page): Promise<void> {
  // Matches both the project-scoped catalog (/api/projects/<pid>/actions/v1/
  // catalog — what the workspace fetches) and the unscoped fallback.
  await page.route('**/actions/v1/catalog', async (route) => {
    const response = await route.fetch();
    const catalog = await response.json();
    const patched = catalog.actions.map((action: Record<string, unknown>) => {
      if (action.kind !== 'media.to_markdown') return action;
      const uiHints = (action.ui_hints as Record<string, unknown> | undefined) ?? {};
      return {
        ...action,
        ui_hints: {
          ...uiHints,
          source_requirements: [
            {
              id: 'source',
              mode: 'column',
              param: 'source',
              label: 'Catalog-patched document source',
              min: 1,
              max: 1,
              accepted_column_types: ['text', 'file'],
              accepted_cell_kinds: ['text', 'blob', 'local_path'],
              message: 'Catalog order should pick text before file.',
            },
          ],
        },
      };
    });
    await route.fulfill({
      status: response.status(),
      contentType: 'application/json',
      body: JSON.stringify({ ...catalog, actions: patched }),
    });
  });
}

async function stubActionRun(
  page: Page,
  pid: string,
): Promise<{
  legacyPosts: V1ActionSpec[];
  v1Posts: V1ActionSpec[];
}> {
  const legacyPosts: V1ActionSpec[] = [];
  const v1Posts: V1ActionSpec[] = [];
  await page.route(`**/api/projects/${pid}/run`, async (route) => {
    legacyPosts.push(route.request().postDataJSON() as V1ActionSpec);
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'catalog source defaults should not use legacy /run' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as V1ActionSpec;
    v1Posts.push(body);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: body.action_id, action_id: 'act-catalog-source-default' },
        status: 'completed',
        project_id: pid,
        run_id: 9971,
        receipt_id: 'receipt-catalog-source-default',
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/*/status`, async (route) => {
    const path = new URL(route.request().url()).pathname.split('/');
    const id = Number(path[path.length - 2]);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        run_id: id,
        status: 'completed',
        total: 1,
        completed: 1,
        failed: 0,
        cost: 0,
        live: false,
      }),
    });
  });
  return { legacyPosts, v1Posts };
}

async function importDocumentColumns(page: Page, pid: string): Promise<number> {
  const sheetId = await importCsv(
    page.request,
    pid,
    'catalog-source-defaults.csv',
    'body,doc\n"Inline document text","/tmp/report.pdf"\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const doc = columns.find((column) => column.name === 'doc');
  expect(doc).toBeTruthy();
  await setColumnType(page.request, pid, doc!.id, 'file');
  return sheetId;
}

// Open only after the served typed catalog has loaded.
async function openToMarkdownAfterCatalogLoad(page: Page, pid: string, sheetId: number): Promise<void> {
  const catalogLoaded = page.waitForResponse((response) => (
    response.url().endsWith('/actions/v1/catalog') && response.status() === 200
  ));
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await catalogLoaded;
  await openAction(page, 'media.to_markdown');
}

test('catalog source requirements choose the default source submitted to v1', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-catalog-source-default'));
  const sheetId = await importDocumentColumns(page, pid);
  await patchToMarkdownSourceRequirement(page);
  const posts = await stubActionRun(page, pid);

  await openToMarkdownAfterCatalogLoad(page, pid, sheetId);
  const sourceSelect = page.getByTestId('field-source');
  await expect(sourceSelect).toBeVisible();
  await expect(sourceSelect).toHaveValue('body');
  await page.getByTestId('generated-action-run').click();

  await expect.poll(() => posts.v1Posts.length, { timeout: 5000 }).toBe(1);
  expect(posts.legacyPosts).toHaveLength(0);
  const posted = posts.v1Posts[0];
  expect(posted.action_id).toBe('media.to_markdown');
  expect(posted.params?.source).toBe('body');
  expect(posted.scope).toEqual({ kind: 'sheet_rows', sheet_id: sheetId });
});

test('explicit source selection still overrides the catalog default', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-catalog-source-explicit'));
  const sheetId = await importDocumentColumns(page, pid);
  await patchToMarkdownSourceRequirement(page);
  const posts = await stubActionRun(page, pid);

  await openToMarkdownAfterCatalogLoad(page, pid, sheetId);
  const sourceSelect = page.getByTestId('field-source');
  await expect(sourceSelect).toHaveValue('body');
  await sourceSelect.selectOption('doc');
  await page.getByTestId('generated-action-run').click();

  await expect.poll(() => posts.v1Posts.length, { timeout: 5000 }).toBe(1);
  expect(posts.legacyPosts).toHaveLength(0);
  const posted = posts.v1Posts[0];
  expect(posted.action_id).toBe('media.to_markdown');
  expect(posted.params?.source).toBe('doc');
  expect(posted.scope).toEqual({ kind: 'sheet_rows', sheet_id: sheetId });
});
