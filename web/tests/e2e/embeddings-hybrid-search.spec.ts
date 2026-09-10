import { expect, test, type Page } from '@playwright/test';
import { embeddingIndexFixture, embeddingIndexListFixture } from '../support/embeddingIndexFixtures';
import { createProject, importCsv, openDiscoverTab, uniqueName } from './helpers';

// The "+ keyword" hybrid toggle sends an embedding_hybrid query (sheet
// FTS + vector fused by RRF) to the hybrid-preview route, and a saved hybrid search is
// an embedding_hybrid lens. Endpoints stubbed; the RRF fusion + sheet-scoped FTS are
// covered by Python tests (tests/ai/test_embedding_hybrid_search.py).

const INDEX_ID = 'embidx_hy';

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
    ready_items: 2,
    stale_items: 0,
    error_items: 0,
    refresh_needed: false,
    last_refreshed_at: '2026-06-21T00:00:00Z',
    remote: false,
    provider_policy: {
      allow_remote: false,
      allow_remote_automatic_refresh: false,
      max_cost_usd_per_refresh: null,
    },
    space_id: 'sp_hy',
    dimension: 384,
    distance_metric: 'cosine',
  });
}

interface CapturedBody {
  name?: string;
  query: { kind?: string; text?: string; embedding_index_id?: string };
}

async function stub(page: Page, pid: string, sheetId: number) {
  const hybridBodies: CapturedBody[] = [];
  const lensBodies: CapturedBody[] = [];
  const lenses: Array<Record<string, unknown>> = [];

  await page.route(`**/api/projects/${pid}/embeddings/v1/indexes*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(embeddingIndexListFixture(sheetId, [makeIndex(sheetId)])),
    }),
  );

  await page.route(`**/api/projects/${pid}/embeddings/v1/hybrid-preview`, (route) => {
    hybridBodies.push(route.request().postDataJSON() as CapturedBody);
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.embedding_hybrid_preview.v1',
        index_id: INDEX_ID,
        sheet_id: sheetId,
        distance_metric: 'rrf',
        hits: [
          {
            row_id: 2,
            sheet_id: sheetId,
            distance: null,
            score: 0.032,
            vector_rank: 2,
            keyword_rank: 1,
            values: { headline: 'drone strike report' },
          },
        ],
      }),
    });
  });

  await page.route(new RegExp(`/api/projects/${pid}/lenses(\\?[^/]*)?$`), async (route) => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as CapturedBody;
      lensBodies.push(body);
      const lens = {
        id: lenses.length + 1,
        name: body.name,
        sheet_id: sheetId,
        spec: { schema_version: 'frisket.lens.v1', query: body.query, presentation: {} },
        op_id: 10 + lenses.length,
        created_at: '2026-06-21T00:00:00Z',
        updated_at: '2026-06-21T00:00:00Z',
      };
      lenses.push(lens);
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(lens) });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(lenses) });
  });

  return { hybridBodies, lensBodies };
}

async function openPanel(page: Page) {
  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();
}

test('the + keyword toggle sends an embedding_hybrid query and renders fused hits', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-hy'));
  const sheetId = await importCsv(request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const { hybridBodies } = await stub(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openPanel(page);

  await page.getByTestId(`embedding-similar-input-${INDEX_ID}`).fill('drone');
  await page.getByTestId(`embedding-hybrid-toggle-${INDEX_ID}`).check();
  await page.getByTestId(`embedding-similar-search-${INDEX_ID}`).click();

  await expect.poll(() => hybridBodies.length).toBe(1);
  expect(hybridBodies[0].query.kind).toBe('embedding_hybrid');
  expect(hybridBodies[0].query.text).toBe('drone');
  expect(hybridBodies[0].query.embedding_index_id).toBe(INDEX_ID);
  // the fused hit renders
  await expect(page.getByTestId('embedding-similar-hit')).toContainText('drone strike report');
});

test('saving a hybrid search persists an embedding_hybrid lens', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('e2e-hy'));
  const sheetId = await importCsv(request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const { lensBodies } = await stub(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openPanel(page);

  await page.getByTestId(`embedding-similar-input-${INDEX_ID}`).fill('drone');
  await page.getByTestId(`embedding-hybrid-toggle-${INDEX_ID}`).check();
  await page.getByTestId(`embedding-similar-save-search-${INDEX_ID}`).click();

  await expect.poll(() => lensBodies.length).toBe(1);
  expect(lensBodies[0].query.kind).toBe('embedding_hybrid');
  expect(lensBodies[0].query.text).toBe('drone');
});
