import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openDiscoverTab, uniqueName } from './helpers';

const INDEX_ID = 'embidx_create_flow';

const PROVIDERS = [
  {
    provider_id: 'fastembed',
    provider_kind: 'local_process',
    model_id: 'paraphrase-MiniLM',
    label: 'Local text (fastembed)',
    modalities: ['text', 'row'],
    dimensions: [384],
    local: true,
    available: true,
    disabled_reason: null,
    modality_compatible: true,
    privacy: { egress: 'none' },
  },
  {
    provider_id: 'cohere',
    provider_kind: 'platform_api',
    model_id: 'embed-v3',
    label: 'Cohere embed-v3',
    modalities: ['text', 'row'],
    dimensions: [1024],
    local: false,
    available: true,
    disabled_reason: null,
    modality_compatible: true,
    privacy: { egress: 'remote' },
    pricing: {
      policy: 'known_unit_price',
      input_usd_per_million_tokens: 2,
      source_url: 'https://example.com/pricing',
      updated: '2026-08-28',
    },
  },
];

function makeIndex(sheetId: number) {
  return {
    index_id: INDEX_ID,
    name: 'headline/body embeddings',
    sheet_id: sheetId,
    modality: 'text',
    provider_id: 'cohere',
    model_id: 'embed-v3',
    source_columns: ['headline', 'body'],
    status: 'idle',
    total_items: 2,
    ready_items: 0,
    stale_items: 0,
    missing_source_items: 2,
    error_items: 0,
    refresh_needed: true,
    last_refreshed_at: null,
    remote: true,
    provider_kind: 'platform_api',
    provider_policy: {
      allow_remote: true,
      allow_remote_automatic_refresh: true,
      max_cost_usd_per_refresh: 0.25,
    },
    maintenance: { mode: 'manual', schedule: null },
    freshness: {
      reason: 'missing_rows',
      current: 0,
      missing: 2,
      stale: 0,
      error: 0,
      total: 2,
      last_refresh_job_id: null,
      last_refresh_receipt_id: null,
      pending_refresh_job_id: null,
    },
    space_id: 'emb_create_flow',
    dimension: 1024,
    distance_metric: 'cosine',
  };
}

interface CreateBody {
  action_id?: string;
  params?: {
    provider?: string;
    model?: string;
    source_columns?: string[];
    provider_policy?: {
      allow_remote?: boolean;
      allow_remote_automatic_refresh?: boolean;
      max_cost_usd_per_refresh?: number | null;
    };
  };
}

async function installStubs(page: Page, pid: string, sheetId: number) {
  let index: ReturnType<typeof makeIndex> | null = null;
  let lastCreate: CreateBody | null = null;

  await page.route(`**/api/projects/${pid}/embeddings/v1/provider-catalog*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.embedding_provider_catalog.v1',
        modality: 'text',
        source_column_type: 'text',
        providers: PROVIDERS,
      }),
    }),
  );

  await page.route(`**/api/projects/${pid}/embeddings/v1/indexes*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.embedding_index_list.v1',
        sheet_id: sheetId,
        indexes: index ? [index] : [],
      }),
    }),
  );

  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as CreateBody;
    if (body.action_id === 'embedding.index_create') {
      lastCreate = body;
      index = makeIndex(sheetId);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          action: { kind: body.action_id, action_id: 'act-create-flow' },
          status: 'completed',
          project_id: pid,
          run_id: null,
          receipt_id: 'rcpt-create-flow',
          outputs: [
            { kind: 'embedding_index', name: INDEX_ID, ref: { index_id: INDEX_ID } },
          ],
          errors: [],
        }),
      });
      return;
    }
    if (body.action_id === 'embedding.index_refresh') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          action: { kind: body.action_id, action_id: 'act-create-flow-refresh' },
          status: 'completed',
          project_id: pid,
          run_id: null,
          receipt_id: 'rcpt-create-flow-refresh',
          outputs: [
            { kind: 'embedding_index', name: INDEX_ID, ref: { index_id: INDEX_ID } },
          ],
          errors: [],
        }),
      });
      return;
    }
    await route.continue();
  });

  return { createBody: () => lastCreate };
}

async function setup(page: Page) {
  const pid = await createProject(page.request, uniqueName('e2e-emb-create-flow'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'stories.csv',
    'headline,body\ncat,first story\nkitten,second story\n',
  );
  const stubs = await installStubs(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  // Embeddings re-homed into the Discover panel (workbench-ia-right-edge-v1).
  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId('embeddings-panel')).toBeVisible();
  await page.getByTestId('embeddings-add-button').click();
  await expect(page.getByTestId('embeddings-form')).toBeVisible();
  return { stubs };
}

test('remote index creation separates source, egress, cost, and summary review', async ({
  page,
}) => {
  const { stubs } = await setup(page);

  await expect(page.getByTestId('embedding-create-step-source')).toHaveAttribute(
    'aria-current',
    'step',
  );
  const sourceColumns = page.getByTestId('embedding-source-columns');
  await expect(sourceColumns).toHaveCSS('flex-direction', 'column');
  const headlineBox = await page.getByTestId('embedding-source-col-headline').boundingBox();
  const bodyBox = await page.getByTestId('embedding-source-col-body').boundingBox();
  expect(headlineBox).not.toBeNull();
  expect(bodyBox).not.toBeNull();
  expect(bodyBox!.y).toBeGreaterThan(headlineBox!.y + headlineBox!.height);
  await page.getByTestId('embedding-source-col-body').check();
  await page.getByTestId('embedding-model-field').click();
  await page.getByTestId('embedding-model-card-embed-v3').click();

  await page.getByTestId('embedding-create-next').click();
  await expect(page.getByTestId('embedding-create-step-egress')).toHaveAttribute(
    'aria-current',
    'step',
  );
  await expect(page.getByTestId('embedding-remote-controls')).toBeVisible();
  await expect(page.getByTestId('embedding-cost-controls')).toHaveCount(0);
  await expect(page.getByTestId('embedding-create-next')).toBeDisabled();

  await page.getByTestId('embedding-allow-remote').check();
  await page.getByTestId('embedding-create-next').click();
  await expect(page.getByTestId('embedding-create-step-cost')).toHaveAttribute(
    'aria-current',
    'step',
  );
  await expect(page.getByTestId('embedding-cost-controls')).toBeVisible();
  await expect(page.getByTestId('embedding-remote-controls')).toHaveCount(0);
  await expect(page.getByTestId('embedding-cost-estimate-first-run')).toContainText(
    'for 2 rows',
  );
  await expect(page.getByTestId('embedding-cost-estimate-per-100')).toContainText(
    'per 100 rows',
  );
  await expect(page.getByTestId('embedding-auto-refresh-note')).toContainText(
    'does not enable a schedule',
  );

  await page.getByTestId('embedding-allow-auto-refresh').check();
  await expect(page.getByTestId('embedding-create-next')).toBeDisabled();
  await page.getByTestId('embedding-max-cost').fill('0.25');
  await page.getByTestId('embedding-remote-confirm').check();
  await page.getByTestId('embedding-create-next').click();

  await expect(page.getByTestId('embedding-create-step-summary')).toHaveAttribute(
    'aria-current',
    'step',
  );
  await expect(page.getByTestId('embedding-create-summary')).toContainText('2 rows');
  await expect(page.getByTestId('embedding-create-summary-columns')).toContainText(
    'headline, body',
  );
  await expect(page.getByTestId('embedding-create-summary-refresh')).toContainText(
    '$0.25',
  );

  await page.getByTestId('embedding-create').click();
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();

  const body = stubs.createBody();
  expect(body?.action_id).toBe('embedding.index_create');
  expect(body?.params?.provider).toBe('cohere');
  expect(body?.params?.model).toBe('embed-v3');
  expect(body?.params?.source_columns).toEqual(['headline', 'body']);
  expect(body?.params?.provider_policy?.allow_remote).toBe(true);
  expect(body?.params?.provider_policy?.allow_remote_automatic_refresh).toBe(true);
  expect(body?.params?.provider_policy?.max_cost_usd_per_refresh).toBe(0.25);
});
