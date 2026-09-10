import { expect, test, type Page } from '@playwright/test';
import { embeddingIndexFixture, embeddingIndexListFixture } from '../support/embeddingIndexFixtures';
import { createProject, importCsv, openDiscoverTab, uniqueName } from './helpers';

// The embedding model card picker. fastembed is not installed in the
// e2e backend, so the provider-catalog + actions/run endpoints are stubbed (same
// pattern as native-embeddings-ui / embeddings-policy). The catalog projection
// carries the per-model factual metadata (size_gb, dimensions, recommended,
// modality_compatible, available, disabled_reason) the cards render.

// A small catalog: a RECOMMENDED local model, two other local models, an
// unavailable remote one, and a modality-incompatible (image) one with a reason.
const PROVIDERS = [
  {
    provider_id: 'fastembed',
    provider_kind: 'local_process',
    model_id: 'BAAI/bge-base-en-v1.5',
    label: 'BGE base (English)',
    modalities: ['text', 'row'],
    dimensions: [768],
    size_gb: 0.21,
    max_input_tokens: 512,
    recommended: false,
    local: true,
    available: true,
    disabled_reason: null,
    modality_compatible: true,
    privacy: { egress: 'none' },
  },
  {
    provider_id: 'fastembed',
    provider_kind: 'local_process',
    model_id: 'paraphrase-multilingual-MiniLM-L12-v2',
    label: 'Multilingual MiniLM',
    modalities: ['text', 'row'],
    dimensions: [384],
    size_gb: 0.07,
    max_input_tokens: 512,
    recommended: true,
    local: true,
    available: true,
    disabled_reason: null,
    modality_compatible: true,
    privacy: { egress: 'none' },
  },
  {
    provider_id: 'fastembed',
    provider_kind: 'local_process',
    model_id: 'BAAI/bge-large-en-v1.5',
    label: 'BGE large (English)',
    modalities: ['text', 'row'],
    dimensions: [1024],
    size_gb: 1.2,
    max_input_tokens: 512,
    recommended: false,
    local: true,
    available: true,
    disabled_reason: null,
    modality_compatible: true,
    privacy: { egress: 'none' },
  },
  {
    provider_id: 'openai',
    provider_kind: 'platform_api',
    model_id: 'text-embedding-3-small',
    label: 'OpenAI text-embedding-3-small',
    modalities: ['text', 'row'],
    dimensions: [1536],
    size_gb: null,
    max_input_tokens: 8191,
    recommended: false,
    local: false,
    available: false,
    disabled_reason: 'missing OPENAI_API_KEY',
    modality_compatible: true,
    privacy: { egress: 'remote' },
  },
  {
    provider_id: 'fastembed',
    provider_kind: 'local_process',
    model_id: 'jinaai/jina-clip-v1',
    label: 'Jina CLIP (image-text)',
    modalities: ['image'],
    dimensions: [768],
    size_gb: 0.55,
    max_input_tokens: null,
    recommended: false,
    local: true,
    available: true,
    disabled_reason: 'modality_mismatch: needs an image column',
    modality_compatible: false,
    privacy: { egress: 'none' },
  },
  // A configured custom-remote (OpenRouter) entry: a bring-your-own-id affordance
  // with NO fixed dimension (discovered at create). Matches the backend's
  // dimension_discovery_required catalog row.
  {
    provider_id: 'openrouter',
    provider_kind: 'platform_api',
    model_id: '',
    label: 'OpenRouter · custom remote model (dimension discovered at create)',
    modalities: ['text', 'row'],
    dimensions: null,
    size_gb: null,
    max_input_tokens: null,
    recommended: false,
    local: false,
    available: true,
    disabled_reason: null,
    modality_compatible: true,
    dimension_discovery_required: true,
    privacy: { egress: 'remote' },
  },
];

const RECOMMENDED = 'paraphrase-multilingual-MiniLM-L12-v2';
const INDEX_ID = 'embidx_picker';

function makeIndex(sheetId: number, provider: string, model: string) {
  return embeddingIndexFixture({
    index_id: INDEX_ID,
    name: 'headline embeddings',
    sheet_id: sheetId,
    modality: 'text',
    provider_id: provider,
    model_id: model,
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
    space_id: 'emb_picker',
    dimension: 384,
    distance_metric: 'cosine',
  });
}

interface Stubs {
  createBody(): {
    action_id?: string;
    params?: { provider?: string; model?: string; provider_policy?: { allow_remote?: boolean } };
  } | null;
}

async function installStubs(page: Page, pid: string, sheetId: number): Promise<Stubs> {
  let index: ReturnType<typeof makeIndex> | null = null;
  let lastCreate: {
    action_id?: string;
    params?: { provider?: string; model?: string; provider_policy?: { allow_remote?: boolean } };
  } | null = null;

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
      body: JSON.stringify(embeddingIndexListFixture(sheetId, index ? [index] : [])),
    }),
  );

  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as {
      action_id?: string;
      params?: { provider?: string; model?: string; provider_policy?: { allow_remote?: boolean } };
    };
    if (body.action_id === 'embedding.index_create') {
      lastCreate = body;
      index = makeIndex(sheetId, body.params?.provider ?? '', body.params?.model ?? '');
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
    await route.continue();
  });

  return { createBody: () => lastCreate };
}

async function setup(page: Page) {
  const pid = await createProject(page.request, uniqueName('e2e-emb-picker'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const stubs = await installStubs(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  // Embeddings re-homed into the Discover panel (workbench-ia-right-edge-v1).
  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId('embeddings-panel')).toBeVisible();
  await page.getByTestId('embeddings-add-button').click();
  await expect(page.getByTestId('embeddings-form')).toBeVisible();
  return { pid, sheetId, stubs };
}

test('cards render with label, dimension, and size', async ({ page }) => {
  await setup(page);
  await page.getByTestId('embedding-model-field').click();
  await expect(page.getByTestId('embedding-model-popover')).toBeVisible();

  const rec = page.getByTestId(`embedding-model-card-${RECOMMENDED}`);
  await expect(rec).toContainText('Multilingual MiniLM');
  await expect(rec).toContainText('384-dim');
  await expect(rec).toContainText('0.07 GB');
  await expect(rec).toContainText('on your machine');
});

test('recommended model is first, badged, and selected by default', async ({ page }) => {
  await setup(page);
  // Selected-by-default shows on the field BEFORE opening.
  await expect(page.getByTestId('embedding-model-field')).toContainText('Multilingual MiniLM');

  await page.getByTestId('embedding-model-field').click();
  const cards = page.locator('[data-testid^="embedding-model-card-"]');
  // The first rendered card is the recommended one.
  await expect(cards.first()).toHaveAttribute(
    'data-testid',
    `embedding-model-card-${RECOMMENDED}`,
  );
  await expect(page.getByTestId('embedding-model-recommended-badge')).toBeVisible();
  await expect(page.getByTestId(`embedding-model-card-${RECOMMENDED}`)).toHaveAttribute(
    'aria-pressed',
    'true',
  );
});

test('a modality-incompatible / unavailable model is disabled with its reason', async ({
  page,
}) => {
  await setup(page);
  await page.getByTestId('embedding-model-field').click();

  // image-text model is modality-incompatible with a text column -> disabled card.
  const incompatible = page.getByTestId('embedding-model-card-disabled-jinaai/jina-clip-v1');
  await expect(incompatible).toBeVisible();
  await expect(incompatible).toContainText('modality_mismatch');
  await expect(incompatible).toBeDisabled();

  // the unavailable remote model is also disabled with its reason.
  const unavailable = page.getByTestId(
    'embedding-model-card-disabled-text-embedding-3-small',
  );
  await expect(unavailable).toContainText('OPENAI_API_KEY');
  await expect(unavailable).toBeDisabled();
});

test('selecting a card flows provider + model into index_create', async ({ page }) => {
  const { stubs } = await setup(page);
  await page.getByTestId('embedding-model-field').click();
  await page.getByTestId('embedding-model-card-BAAI/bge-base-en-v1.5').click();
  // popover closes; the field reflects the chosen model.
  await expect(page.getByTestId('embedding-model-popover')).toHaveCount(0);
  await expect(page.getByTestId('embedding-model-field')).toContainText('BGE base');

  await page.getByTestId('embedding-create-next').click();
  await page.getByTestId('embedding-create').click();
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();

  const body = stubs.createBody();
  expect(body?.action_id).toBe('embedding.index_create');
  expect(body?.params?.provider).toBe('fastembed');
  expect(body?.params?.model).toBe('BAAI/bge-base-en-v1.5');
});

test('a custom model id passes through as params.model', async ({ page }) => {
  const { stubs } = await setup(page);
  await page.getByTestId('embedding-model-field').click();
  await page
    .getByTestId('embedding-model-custom-input')
    .fill('codefuse-ai/F2LLM-v2-14B');
  // close the popover (click the field opener again) and create.
  await page.getByTestId('embedding-model-field').click();
  await page.getByTestId('embedding-create-next').click();
  await page.getByTestId('embedding-create').click();
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();

  const body = stubs.createBody();
  expect(body?.params?.model).toBe('codefuse-ai/F2LLM-v2-14B');
  // still carries a provider so the backend can validate the custom id.
  expect(body?.params?.provider).toBe('fastembed');
});

test('a custom-remote (OpenRouter) card pins its provider for a typed model id', async ({
  page,
}) => {
  const { stubs } = await setup(page);
  await page.getByTestId('embedding-model-field').click();

  // The OpenRouter card is shown honestly: no dimension, a discovery note.
  const orouter = page.getByTestId('embedding-model-card-');
  await expect(orouter).toContainText('OpenRouter');
  await expect(orouter).toContainText('dimension found at create');
  await expect(orouter).toContainText('remote · openrouter');

  // Selecting it pins the remote provider; the popover stays open to type an id.
  await orouter.click();
  await page
    .getByTestId('embedding-model-custom-input')
    .fill('openai/text-embedding-3-large');
  // close the popover.
  await page.getByTestId('embedding-model-field').click();

  // A remote model surfaces the allow-remote (egress) review BEFORE create —
  // the UI mirrors the backend's create-time egress gate.
  await page.getByTestId('embedding-create-next').click();
  await expect(page.getByTestId('embedding-allow-remote')).toBeVisible();
  await page.getByTestId('embedding-allow-remote').check();
  await page.getByTestId('embedding-create-next').click();
  await page.getByTestId('embedding-create-next').click();

  await page.getByTestId('embedding-create').click();
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();

  const body = stubs.createBody();
  expect(body?.params?.provider).toBe('openrouter');
  expect(body?.params?.model).toBe('openai/text-embedding-3-large');
  expect(body?.params?.provider_policy?.allow_remote).toBe(true);
});
