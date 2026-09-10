import { expect, test, type Page } from '@playwright/test';
import { embeddingIndexFixture, embeddingIndexListFixture } from '../support/embeddingIndexFixtures';
import { createProject, importCsv, openDiscoverTab, uniqueName } from './helpers';

// A composed semantic search (drone +"crop spraying" -(defense:0.4))
// parses in the web to STRUCTURED weighted terms; the similarity-preview POST and the
// saved-lens POST carry `anchor.terms`, never the raw query string. The embedding +
// lens endpoints are stubbed (deterministic, embedder-independent); the backend
// contract (resolver weighted-sum, lens accept) is covered by Python tests.

const INDEX_ID = 'embidx_cts';
const COMPOSED = 'drone +"crop spraying" -(defense:0.4)';
const EXPECTED_TERMS = [
  { text: 'drone', weight: 1 },
  { text: 'crop spraying', weight: 1 },
  { text: 'defense', weight: -0.4 },
];

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
    space_id: 'sp_cts',
    dimension: 384,
    distance_metric: 'cosine',
  });
}

interface CapturedBody {
  name?: string;
  query: { anchor: Record<string, unknown> };
}

async function stub(page: Page, pid: string, sheetId: number) {
  const previewBodies: CapturedBody[] = [];
  const lensBodies: CapturedBody[] = [];
  const lenses: Array<Record<string, unknown>> = [];

  await page.route(`**/api/projects/${pid}/embeddings/v1/indexes*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(embeddingIndexListFixture(sheetId, [makeIndex(sheetId)])),
    }),
  );

  await page.route(`**/api/projects/${pid}/embeddings/v1/similarity-preview`, (route) => {
    previewBodies.push(route.request().postDataJSON() as CapturedBody);
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.embedding_similarity_preview.v1',
        index_id: INDEX_ID,
        space_id: 'sp_cts',
        sheet_id: sheetId,
        distance_metric: 'cosine',
        anchor: { kind: 'manual_text_query' },
        limit: 5,
        hits: [
          {
            row_id: 2,
            sheet_id: sheetId,
            distance: 0.1,
            score: 0.9,
            values: { headline: 'ag drone' },
          },
        ],
      }),
    });
  });

  // POST /lenses (save search) + GET /lenses; stubbed objects carry created/updated_at.
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

  return { previewBodies, lensBodies };
}

async function openPanel(page: Page) {
  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();
}

test('a composed query sends structured weighted terms, not the raw string', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-cts'));
  const sheetId = await importCsv(request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const { previewBodies, lensBodies } = await stub(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openPanel(page);

  await page.getByTestId(`embedding-similar-input-${INDEX_ID}`).fill(COMPOSED);
  await page.getByTestId(`embedding-similar-search-${INDEX_ID}`).click();

  await expect.poll(() => previewBodies.length).toBe(1);
  const anchor = previewBodies[0].query.anchor;
  expect(anchor.kind).toBe('manual_text_query');
  expect(anchor.text).toBeUndefined(); // the raw query string did NOT leak to the wire
  expect(anchor.terms).toEqual(EXPECTED_TERMS);

  // saving the search persists a manual_text_query lens carrying the SAME terms
  await page.getByTestId(`embedding-similar-save-search-${INDEX_ID}`).click();
  await expect.poll(() => lensBodies.length).toBe(1);
  const lensAnchor = lensBodies[0].query.anchor;
  expect(lensAnchor.kind).toBe('manual_text_query');
  expect(lensAnchor.terms).toEqual(EXPECTED_TERMS);
  expect(lensAnchor.text).toBeUndefined();
});

test('a plain phrase still rides the legacy single text', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('e2e-cts'));
  const sheetId = await importCsv(request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const { previewBodies } = await stub(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openPanel(page);

  await page.getByTestId(`embedding-similar-input-${INDEX_ID}`).fill('kitten');
  await page.getByTestId(`embedding-similar-search-${INDEX_ID}`).click();

  await expect.poll(() => previewBodies.length).toBe(1);
  const anchor = previewBodies[0].query.anchor;
  expect(anchor.kind).toBe('manual_text_query');
  expect(anchor.text).toBe('kitten'); // plain phrase -> legacy single text
  expect(anchor.terms).toBeUndefined();
});

test('an !exclude term sends a structured hard-NOT exclude', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-cts'));
  const sheetId = await importCsv(request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const { previewBodies } = await stub(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openPanel(page);

  await page
    .getByTestId(`embedding-similar-input-${INDEX_ID}`)
    .fill('drone !defense !(military:0.6)');
  await page.getByTestId(`embedding-similar-search-${INDEX_ID}`).click();

  await expect.poll(() => previewBodies.length).toBe(1);
  const anchor = previewBodies[0].query.anchor;
  expect(anchor.terms).toEqual([{ text: 'drone', weight: 1 }]);
  // !defense (default threshold) + !(military:0.6) (explicit cosine-score threshold)
  expect(anchor.exclude).toEqual([
    { text: 'defense' },
    { text: 'military', threshold: 0.6 },
  ]);
});
