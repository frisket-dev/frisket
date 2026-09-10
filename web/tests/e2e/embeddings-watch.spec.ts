import { expect, test, type Page } from '@playwright/test';
import { embeddingIndexFixture, embeddingIndexListFixture } from '../support/embeddingIndexFixtures';
import { createProject, importCsv, openDiscoverTab, uniqueName } from './helpers';

// Create a "rows similar to this row" watch from the show-similar
// preview, and surface a blocked watch's Refresh affordance in WatchesPanel. The
// embedding + watch endpoints are stubbed (deterministic, embedder-independent);
// the backend contracts are covered by Python tests.

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
    space_id: 'emb_e2e',
    dimension: 384,
    distance_metric: 'cosine',
  });
}

async function stubEmbeddings(page: Page, pid: string, sheetId: number) {
  const watchPosts: Record<string, unknown>[] = [];
  const actionPosts: Record<string, unknown>[] = [];

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
  await page.route(`**/api/projects/${pid}/watches`, async (route) => {
    if (route.request().method() === 'POST') {
      watchPosts.push(route.request().postDataJSON() as Record<string, unknown>);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: 1,
          name: 'w',
          query: {},
          created_at: '2026-06-21T00:00:00Z',
          updated_at: '2026-06-21T00:00:00Z',
        }),
      });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as { action_id?: string };
    if (typeof body.action_id === 'string' && body.action_id.startsWith('embedding.')) {
      actionPosts.push(body);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          action: { kind: body.action_id, action_id: 'a' },
          status: 'completed',
          project_id: pid,
          run_id: null,
          receipt_id: 'r',
          outputs: [{ kind: 'embedding_index', name: INDEX_ID, ref: { index_id: INDEX_ID } }],
          errors: [],
        }),
      });
      return;
    }
    await route.continue();
  });
  return { watchPosts, actionPosts };
}

test('Watch this row creates an embedding_similarity watch from show-similar', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-watch'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const { watchPosts } = await stubEmbeddings(page, pid, sheetId);
  await page.goto(`/p/${pid}`);

  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();

  await page.getByTestId(`embedding-similar-input-${INDEX_ID}`).fill('kitty');
  await page.getByTestId(`embedding-similar-search-${INDEX_ID}`).click();
  await expect(page.getByTestId('embedding-watch-hit-2')).toBeVisible();
  await page.getByTestId('embedding-watch-hit-2').click();

  await expect(page.getByTestId('embedding-watch-hit-2')).toContainText('Watching');
  await expect.poll(() => watchPosts.length).toBe(1);
  const query = watchPosts[0].query as Record<string, unknown>;
  expect(query.kind).toBe('embedding_similarity');
  expect(query.embedding_index_id).toBe(INDEX_ID);
  expect((query.anchor as Record<string, unknown>).row_id).toBe(2);
});

test('a blocked embedding watch shows a Refresh link that refreshes the index', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-watch'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const { actionPosts } = await stubEmbeddings(page, pid, sheetId);

  // a blocked embedding_similarity watch (its index needs a refresh)
  await page.route(`**/api/projects/${pid}/watches`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          id: 7,
          name: 'Similar to kitten',
          scope: 'sheet',
          sheet_id: sheetId,
          query: { kind: 'embedding_similarity', embedding_index_id: INDEX_ID, anchor: { kind: 'row', row_id: 2 } },
          enabled: true,
          created_at: '2026-06-21T00:00:00Z',
          updated_at: '2026-06-21T00:00:00Z',
          latest_run: {
            id: 1,
            watch_id: 7,
            status: 'error',
            error_code: 'embedding_index_incomplete',
            matched_rows: 0,
            new_rows: 0,
            started_at: '2026-06-21T00:00:00Z',
          },
        },
      ]),
    }),
  );
  await page.goto(`/p/${pid}`);
  await openDiscoverTab(page, 'Watches');

  await expect(page.getByTestId('watch-latest-run')).toContainText('embedding_index_incomplete');
  await page.getByTestId('watch-refresh-index-7').click();
  // refresh -> embedding.index_refresh action posted
  await expect.poll(() => actionPosts.map((p) => p.action_id)).toContain('embedding.index_refresh');
});
