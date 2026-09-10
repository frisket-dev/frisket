import { expect, test, type Locator, type Page } from '@playwright/test';
import { embeddingIndexFixture, embeddingIndexListFixture } from '../support/embeddingIndexFixtures';
import { createProject, importCsv, openDiscoverTab, uniqueName } from './helpers';

// The embedding endpoints are stubbed (like media-engine-tier-picker) so the spec
// is deterministic and independent of whether a local embedder is installed in the
// e2e backend. The backend contracts themselves are covered by Python tests.

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
    privacy: { egress: 'none' },
  },
  {
    provider_id: 'openai',
    provider_kind: 'platform_api',
    model_id: 'text-embedding-3-small',
    label: 'OpenAI text-embedding-3-small',
    modalities: ['text', 'row'],
    dimensions: [1536],
    local: false,
    available: false,
    disabled_reason: 'missing OPENAI_API_KEY',
    privacy: { egress: 'remote' },
  },
  {
    // an AVAILABLE remote provider (a key is configured) — selectable, so the UI
    // can demand privacy/cost confirmation before automatic refresh.
    provider_id: 'cohere',
    provider_kind: 'platform_api',
    model_id: 'embed-v3',
    label: 'Cohere embed-v3',
    modalities: ['text', 'row'],
    dimensions: [1024],
    local: false,
    available: true,
    disabled_reason: null,
    privacy: { egress: 'remote' },
  },
];

const INDEX_ID = 'embidx_e2e';

function makeIndex(sheetId: number) {
  return embeddingIndexFixture({
    index_id: INDEX_ID,
    name: 'headline embeddings',
    sheet_id: sheetId,
    modality: 'text',
    provider_id: 'fastembed',
    model_id: 'paraphrase-MiniLM',
    source_columns: ['headline'],
    status: 'idle',
    total_items: 2,
    ready_items: 0,
    stale_items: 0,
    error_items: 0,
    refresh_needed: true,
    last_refreshed_at: null,
    remote: false,
    provider_policy: {
      allow_remote: false,
      allow_remote_automatic_refresh: false,
      max_cost_usd_per_refresh: null,
    },
    space_id: 'emb_e2e',
    dimension: 384,
    distance_metric: 'cosine',
  });
}

interface Stubs {
  setStale(value: boolean): void;
  seedIndex(): void;
}

async function expectEmbeddingPanelContribution(contribution: Locator) {
  await expect(contribution).toHaveAttribute(
    'data-schema-version',
    'frisket.workbench.panel.v1',
  );
  await expect(contribution).toHaveAttribute(
    'data-contribution-id',
    'frisket.embeddings.panel.indexes',
  );
  await expect(contribution).toHaveAttribute('data-host', 'leftSidebar');
  await expect(contribution).toHaveAttribute('data-mode', 'panel');
  await expect(contribution).toHaveAttribute(
    'data-runtime-component-key',
    'embeddings.panels.EmbeddingsPanel',
  );

  const capabilitiesAttr = await contribution.getAttribute('data-required-capabilities');
  expect(capabilitiesAttr).not.toBeNull();
  const capabilities = capabilitiesAttr?.split(/\s+/).filter(Boolean).sort() ?? [];
  expect(capabilities).toEqual([
    'embedding.analysis.run',
    'embedding.index.create',
    'embedding.index.export',
    'embedding.index.list',
    'embedding.index.policy.update',
    'embedding.index.refresh',
    'embedding.lens.open',
    'embedding.lens.save',
    'embedding.provider.catalog.read',
    'embedding.similarity.preview',
    'host.navigation.openSheet',
    'watch.create',
  ].sort());
}

async function installEmbeddingStubs(
  page: Page,
  pid: string,
  sheetId: number,
): Promise<Stubs> {
  let index: ReturnType<typeof makeIndex> | null = null;
  let stale = false;

  await page.route(`**/api/projects/${pid}/embeddings/v1/provider-catalog*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.embedding_provider_catalog.v1',
        modality: 'text',
        source_column_type: null,
        providers: PROVIDERS,
      }),
    });
  });

  await page.route(`**/api/projects/${pid}/embeddings/v1/indexes*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(embeddingIndexListFixture(sheetId, index ? [index] : [])),
    });
  });

  await page.route(`**/api/projects/${pid}/embeddings/v1/similarity-preview`, async (route) => {
    if (stale) {
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: {
            code: 'embedding_source_stale',
            message: 'source content changed since it was embedded',
            field: 'params.index_id',
          },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.embedding_similarity_preview.v1',
        index_id: INDEX_ID,
        space_id: 'emb_e2e',
        sheet_id: sheetId,
        distance_metric: 'cosine',
        anchor: { kind: 'manual_text_query' },
        limit: 5,
        hits: [
          { row_id: 2, sheet_id: sheetId, distance: 0.1, score: 0.9, values: { headline: 'kitten' } },
        ],
      }),
    });
  });

  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as { action_id?: string };
    if (body.action_id === 'embedding.index_create') {
      index = makeIndex(sheetId);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          action: { kind: body.action_id, action_id: 'act-create' },
          status: 'completed',
          project_id: pid,
          run_id: null,
          receipt_id: 'rcpt-create',
          outputs: [{ kind: 'embedding_index', name: INDEX_ID, ref: { index_id: INDEX_ID } }],
          errors: [],
        }),
      });
      return;
    }
    if (body.action_id === 'embedding.index_refresh') {
      if (index) {
        index.refresh_needed = false;
        index.ready_items = index.total_items;
        index.last_refreshed_at = '2026-06-20T12:00:00Z';
      }
      stale = false;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          action: { kind: body.action_id, action_id: 'act-refresh' },
          status: 'completed',
          project_id: pid,
          run_id: null,
          receipt_id: 'rcpt-refresh',
          outputs: [{ kind: 'embedding_index', name: INDEX_ID, ref: { index_id: INDEX_ID } }],
          errors: [],
        }),
      });
      return;
    }
    await route.continue();
  });

  return {
    setStale: (value: boolean) => {
      stale = value;
    },
    seedIndex: () => {
      index = makeIndex(sheetId);
    },
  };
}

async function setup(page: Page) {
  const pid = await createProject(page.request, uniqueName('e2e-embeddings'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const stubs = await installEmbeddingStubs(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  // Wait for the grid to settle before interacting with the Discover panel —
  // a late grid reflow otherwise destabilizes the panel's form controls.
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  // Embeddings re-homed into the Discover panel (workbench-ia-right-edge-v1).
  await openDiscoverTab(page, 'Embeddings');
  await expectEmbeddingPanelContribution(
    page.getByTestId('workbench-contribution-frisket-embeddings-panel-indexes'),
  );
  await expect(page.getByTestId('embeddings-panel')).toBeVisible();
  return { pid, sheetId, stubs };
}

test('provider picker shows available + disabled-remote and creates a local index', async ({ page }) => {
  await setup(page);

  await page.getByTestId('embeddings-empty').waitFor();
  await page.getByTestId('embeddings-add-button').click();
  await expect(page.getByTestId('embeddings-form')).toBeVisible();

  // open the card picker
  await page.getByTestId('embedding-model-field').click();
  await expect(page.getByTestId('embedding-model-popover')).toBeVisible();
  // unavailable remote model stays visible as a DISABLED card WITH its reason
  await expect(
    page.getByTestId('embedding-model-card-disabled-text-embedding-3-small'),
  ).toContainText('OPENAI_API_KEY');

  // pick the local model and create (the first source column is pre-selected)
  await page.getByTestId('embedding-model-card-paraphrase-MiniLM').click();
  await expect(page.getByTestId('embedding-source-col-headline')).toBeChecked();
  await page.getByTestId('embedding-create-next').click();
  await expect(page.getByTestId('embedding-create-summary')).toContainText('2 rows');
  await page.getByTestId('embedding-create').click();

  // The new index appears and its initial build is CHAINED
  // automatically — no manual "refresh needed" limbo, no second click.
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();
  await expect(page.getByTestId(`embedding-refresh-needed-${INDEX_ID}`)).toHaveCount(0);
  await expect(page.getByTestId(`embedding-status-${INDEX_ID}`)).toContainText('2/2 ready');
});

test('remote provider requires privacy confirmation before automatic refresh', async ({ page }) => {
  await setup(page);
  await page.getByTestId('embeddings-add-button').click();
  await page.getByTestId('embedding-model-field').click();
  // unavailable remote model is visible but cannot be selected (disabled card)
  await expect(
    page.getByTestId('embedding-model-card-disabled-text-embedding-3-small'),
  ).toContainText('OPENAI_API_KEY');
  // an available remote model triggers the privacy/cost confirmation controls
  await page.getByTestId('embedding-model-card-embed-v3').click();
  await page.getByTestId('embedding-create-next').click();

  await expect(page.getByTestId('embedding-remote-controls')).toBeVisible();
  await expect(page.getByTestId('embedding-create-next')).toBeDisabled();
  await page.getByTestId('embedding-allow-remote').check();
  await page.getByTestId('embedding-create-next').click();
  await expect(page.getByTestId('embedding-cost-controls')).toBeVisible();
  await page.getByTestId('embedding-allow-auto-refresh').check();
  // the control is PRE-AUTHORIZATION, not a schedule — the copy must say so
  await expect(page.getByTestId('embedding-auto-refresh-note')).toContainText(
    'does not enable a schedule',
  );
  // pre-authorizing unattended remote refresh demands a cost ceiling + confirmation
  await expect(page.getByTestId('embedding-auto-confirm')).toBeVisible();
});

test('refresh clears refresh-needed and show-similar returns hits', async ({ page }) => {
  const { stubs } = await setup(page);
  stubs.seedIndex();
  await page.reload();
  // Discover tab + panel open-state persist across reload; the panel body is
  // visible without re-clicking any toggle.
  await expect(page.getByTestId('embeddings-panel')).toBeVisible();

  await expect(page.getByTestId(`embedding-refresh-needed-${INDEX_ID}`)).toBeVisible();
  await page.getByTestId(`embedding-refresh-${INDEX_ID}`).click();
  await expect(page.getByTestId(`embedding-refresh-needed-${INDEX_ID}`)).toHaveCount(0);
  await expect(page.getByTestId(`embedding-status-${INDEX_ID}`)).toContainText('2/2 ready');

  await page.getByTestId(`embedding-similar-input-${INDEX_ID}`).fill('kitty');
  await page.getByTestId(`embedding-similar-search-${INDEX_ID}`).click();
  await expect(page.getByTestId(`embedding-similar-hits-${INDEX_ID}`)).toContainText('kitten');
});

test('stale embeddings surface a refresh-needed affordance, not a generic error', async ({ page }) => {
  const { stubs } = await setup(page);
  stubs.seedIndex();
  await page.reload();
  await expect(page.getByTestId('embeddings-panel')).toBeVisible();
  await page.getByTestId(`embedding-refresh-${INDEX_ID}`).click();

  // source content changed since embedding -> the next similar query is stale
  stubs.setStale(true);
  await page.getByTestId(`embedding-similar-input-${INDEX_ID}`).fill('kitty');
  await page.getByTestId(`embedding-similar-search-${INDEX_ID}`).click();

  const banner = page.getByTestId(`embedding-similar-stale-${INDEX_ID}`);
  await expect(banner).toBeVisible();
  await expect(banner).toContainText('refresh needed');

  // the banner's Refresh re-embeds; a follow-up query then succeeds
  await page.getByTestId(`embedding-similar-refresh-${INDEX_ID}`).click();
  await page.getByTestId(`embedding-similar-input-${INDEX_ID}`).fill('kitty');
  await page.getByTestId(`embedding-similar-search-${INDEX_ID}`).click();
  await expect(page.getByTestId(`embedding-similar-hits-${INDEX_ID}`)).toContainText('kitten');
});
