import { expect, test, type Page } from '@playwright/test';
import { embeddingIndexFixture, embeddingIndexListFixture } from '../support/embeddingIndexFixtures';
import { createProject, importCsv, openDiscoverTab, uniqueName } from './helpers';

// Save a "rows similar to this row" lens from a
// show-similar hit, list it, open it (resolve), and render the ranked row-set
// WITH per-row distance/score. A stale anchor resolve surfaces the typed
// refresh-needed message, not a crash. The embedding + lens endpoints are
// stubbed (deterministic, embedder-independent); the backend lens contract is
// covered by Python tests (tests/ai/test_embedding_lens.py).

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
    total_items: 3,
    ready_items: 3,
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
    space_id: 'emb_e2e',
    dimension: 384,
    distance_metric: 'cosine',
  });
}

interface LensStubs {
  lensPosts: Record<string, unknown>[];
  setAnchorStale(value: boolean): void;
  /** Force the next resolve to 400 with this typed code (e.g. embedding_index_incomplete). */
  setResolveError(code: string | null): void;
}

async function stubEmbeddingsAndLenses(
  page: Page,
  pid: string,
  sheetId: number,
): Promise<LensStubs> {
  const lensPosts: Record<string, unknown>[] = [];
  // The stubbed lens store: POST appends, GET lists, resolve serves a ranked set.
  const lenses: Array<Record<string, unknown>> = [];
  let anchorStale = false;
  let resolveErrorCode: string | null = null;

  await page.route(`**/api/projects/${pid}/embeddings/v1/indexes*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(embeddingIndexListFixture(sheetId, [makeIndex(sheetId)])),
    }),
  );

  await page.route(`**/api/projects/${pid}/embeddings/v1/similarity-preview`, (route) =>
    route.fulfill({
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
        hits: [{ row_id: 2, sheet_id: sheetId, distance: 0.1, score: 0.9, values: { headline: 'kitten' } }],
      }),
    }),
  );

  // POST + GET /lenses. Stubbed wire lens objects MUST carry created_at/updated_at
  // so the toLens mapper does not choke (prior lens-like spec bug).
  // Match POST /lenses and GET /lenses?sheet_id=... with one regex (the glob
  // form mishandles the query string + the '?' wildcard char).
  await page.route(new RegExp(`/api/projects/${pid}/lenses(\\?[^/]*)?$`), async (route) => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      lensPosts.push(body);
      const lens = {
        id: lenses.length + 1,
        name: body.name,
        sheet_id: sheetId,
        spec: {
          schema_version: 'frisket.lens.v1',
          query: body.query,
          presentation: body.presentation ?? {},
        },
        op_id: 10 + lenses.length,
        created_at: '2026-06-21T00:00:00Z',
        updated_at: '2026-06-21T00:00:00Z',
      };
      lenses.push(lens);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(lens),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(lenses),
    });
  });

  // GET /lenses/{id}/resolve — ranked row_ids + per-row distance/score, or a
  // typed embedding_anchor_stale 400 when the anchor row drifted.
  await page.route(`**/api/projects/${pid}/lenses/*/resolve*`, async (route) => {
    const errorCode = resolveErrorCode ?? (anchorStale ? 'embedding_anchor_stale' : null);
    if (errorCode) {
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: {
            code: errorCode,
            message: `index not searchable (${errorCode})`,
            field: 'embedding_index_id',
          },
        }),
      });
      return;
    }
    const url = new URL(route.request().url());
    const lensId = Number(url.pathname.split('/').slice(-2)[0]);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        lens_id: lensId,
        schema_version: 'frisket.query_preview.v1',
        sheet_id: sheetId,
        query: { kind: 'embedding_similarity', embedding_index_id: INDEX_ID, anchor: { kind: 'row', row_id: 2 } },
        query_hash: 'sha256:lens-e2e',
        row_ids: [3, 1],
        row_count: 2,
        total: 2,
        offset: 0,
        limit: 20,
        evaluator: 'embedding_similarity',
        scores: {
          '3': { distance: 0.12, score: 0.88 },
          '1': { distance: 0.34, score: 0.66 },
        },
      }),
    });
  });

  return {
    lensPosts,
    setAnchorStale: (value: boolean) => {
      anchorStale = value;
    },
    setResolveError: (code: string | null) => {
      resolveErrorCode = code;
    },
  };
}

async function openShowSimilar(page: Page) {
  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();
  await page.getByTestId(`embedding-similar-input-${INDEX_ID}`).fill('kitty');
  await page.getByTestId(`embedding-similar-search-${INDEX_ID}`).click();
  await expect(page.getByTestId('embedding-lens-save-hit-2')).toBeVisible();
}

test('saving a view from a show-similar hit POSTs a row-anchor embedding_similarity lens', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-lens'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\npuppy\n');
  const { lensPosts } = await stubEmbeddingsAndLenses(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openShowSimilar(page);

  await page.getByTestId('embedding-lens-save-hit-2').click();
  await expect(page.getByTestId('embedding-lens-save-hit-2')).toContainText('Saved');

  await expect.poll(() => lensPosts.length).toBe(1);
  const query = lensPosts[0].query as Record<string, unknown>;
  expect(query.kind).toBe('embedding_similarity');
  expect(query.embedding_index_id).toBe(INDEX_ID);
  expect((query.anchor as Record<string, unknown>).kind).toBe('row');
  expect((query.anchor as Record<string, unknown>).row_id).toBe(2);
  // the saved query carries an EXPLICIT limit = the grid lens window (500), so the
  // candidate top-K matches the window — without it the QuerySpec default (50) would
  // silently cap the grid at 50 rows.
  expect(query.limit).toBe(500);
});

test('the saved lens appears in the lens list', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-lens'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\npuppy\n');
  await stubEmbeddingsAndLenses(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openShowSimilar(page);

  await page.getByTestId('embedding-lens-save-hit-2').click();
  // the lens list reloads and shows the new lens (id 1)
  await expect(page.getByTestId('embedding-lens-1')).toBeVisible();
  await expect(page.getByTestId('embedding-lens-1')).toContainText('Similar to');
  await expect(page.getByTestId('embedding-lens-open-1')).toBeVisible();
});

test('opening a lens resolves it and renders the ranked rows with distance/score', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-lens'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\npuppy\n');
  await stubEmbeddingsAndLenses(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openShowSimilar(page);

  await page.getByTestId('embedding-lens-save-hit-2').click();
  await page.getByTestId('embedding-lens-open-1').click();

  const results = page.getByTestId('embedding-lens-results-1');
  await expect(results).toBeVisible();
  // ranked rows surface their numeric distance + score (what the watch path drops)
  await expect(page.getByTestId('embedding-lens-result-row-3')).toContainText('0.120');
  await expect(page.getByTestId('embedding-lens-result-row-3')).toContainText('0.880');
  await expect(page.getByTestId('embedding-lens-result-row-1')).toContainText('0.340');
  await expect(page.getByTestId('embedding-lens-result-row-1')).toContainText('0.660');
});

test('a stale-anchor resolve shows a refresh-needed message rather than a crash', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-lens'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\npuppy\n');
  const { setAnchorStale } = await stubEmbeddingsAndLenses(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openShowSimilar(page);

  await page.getByTestId('embedding-lens-save-hit-2').click();
  await expect(page.getByTestId('embedding-lens-1')).toBeVisible();

  setAnchorStale(true);
  await page.getByTestId('embedding-lens-open-1').click();

  const err = page.getByTestId('embedding-lens-resolve-error-1');
  await expect(err).toBeVisible();
  await expect(err).toContainText('out of date');
  // no results rendered and the panel did not crash
  await expect(page.getByTestId('embedding-lens-results-1')).toHaveCount(0);
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();
});

test('an incomplete-index resolve shows refresh-needed, not a raw error', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-lens'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\npuppy\n');
  const { setResolveError } = await stubEmbeddingsAndLenses(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openShowSimilar(page);

  await page.getByTestId('embedding-lens-save-hit-2').click();
  await expect(page.getByTestId('embedding-lens-1')).toBeVisible();

  // an appended unembedded row -> the backend gate returns embedding_index_incomplete
  setResolveError('embedding_index_incomplete');
  await page.getByTestId('embedding-lens-open-1').click();

  const err = page.getByTestId('embedding-lens-resolve-error-1');
  await expect(err).toBeVisible();
  await expect(err).toContainText('out of date'); // refresh-needed, not a raw error code
  await expect(page.getByTestId('embedding-lens-results-1')).toHaveCount(0);
});
