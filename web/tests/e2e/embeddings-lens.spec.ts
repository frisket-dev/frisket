import { expect, test, type Page } from '@playwright/test';
import type { HttpContractSuccessResponse } from '../../src/api/httpContract';
import { embeddingIndexFixture, embeddingIndexListFixture } from '../support/embeddingIndexFixtures';
import { createProject, importCsv, openDiscoverTab, uniqueName } from './helpers';

// The Embeddings panel lists the saved views that rank rows by an index. Opening
// one resolves it and shows the ranked rows with their scores; an index that
// needs a refresh says so instead of showing a raw error. The index, view list
// and resolve endpoints are stubbed with their contract shapes, since no
// embedding model runs keyless.

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

type ViewList = HttpContractSuccessResponse<'tenant.list_views.get'>;
type ViewResolved = HttpContractSuccessResponse<'tenant.resolve_view.get'>;

/** Stub one index with one saved similarity view. The returned setter makes
 *  the next resolve answer 400 with a typed code. */
async function stubIndexWithView(
  page: Page,
  pid: string,
  sheetId: number,
): Promise<(code: string | null) => void> {
  const views: ViewList = [{
    id: 1,
    name: 'Similar to kitten',
    sheet_id: sheetId,
    spec: {
      selection: {
        scope: { kind: 'sheet', sheet_id: sheetId },
        filters: [],
        order: [{ score: 'similarity', dir: 'desc' }],
        limit: null,
        similarity: { embedding_index_id: INDEX_ID, anchor: { kind: 'row', row_id: 2 } },
      },
      presentation: {},
    },
    change_id: 10,
    unavailable_reason: null,
    created_at: '2026-06-21T00:00:00Z',
    updated_at: '2026-06-21T00:00:00Z',
  }];
  let resolveErrorCode: string | null = null;

  await page.route(`**/api/projects/${pid}/embeddings/v1/indexes*`, (route) =>
    route.fulfill({ json: embeddingIndexListFixture(sheetId, [makeIndex(sheetId)]) }));

  await page.route(new RegExp(`/api/projects/${pid}/views(\\?[^/]*)?$`), (route) =>
    route.request().method() === 'GET' ? route.fulfill({ json: views }) : route.continue());

  await page.route(`**/api/projects/${pid}/views/1/resolve*`, async (route) => {
    if (resolveErrorCode) {
      await route.fulfill({
        status: 400,
        json: {
          detail: {
            code: resolveErrorCode,
            message: `index not searchable (${resolveErrorCode})`,
            field: 'embedding_index_id',
          },
        },
      });
      return;
    }
    const resolved: ViewResolved = {
      view_id: 1,
      query_hash: 'sha256:view-e2e',
      refs: [
        { sheet_id: sheetId, row_id: 3, search_score: 0.12, similarity_score: 0.88, relevance_score: null },
        { sheet_id: sheetId, row_id: 1, search_score: 0.34, similarity_score: 0.66, relevance_score: null },
      ],
      total: 2,
      offset: 0,
      limit: 20,
    };
    await route.fulfill({ json: resolved });
  });

  return (code) => {
    resolveErrorCode = code;
  };
}

async function openEmbeddingViews(page: Page) {
  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();
  await expect(page.getByTestId('embedding-view-1')).toContainText('Similar to kitten');
  await expect(page.getByTestId('embedding-view-open-1')).toBeVisible();
}

test('opening a saved similarity view lists its ranked rows with their scores', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-view'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\npuppy\n');
  await stubIndexWithView(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openEmbeddingViews(page);
  await page.getByTestId('embedding-view-open-1').click();

  const results = page.getByTestId('embedding-view-results-1');
  await expect(results).toBeVisible();
  await expect(results.locator('li')).toHaveCount(2);
  await expect(results.locator('li').first()).toHaveAttribute('data-testid', 'embedding-view-result-row-3');
  await expect(page.getByTestId('embedding-view-result-row-3')).toContainText('0.120');
  await expect(page.getByTestId('embedding-view-result-row-3')).toContainText('0.880');
  await expect(page.getByTestId('embedding-view-result-row-1')).toContainText('0.340');
  await expect(page.getByTestId('embedding-view-result-row-1')).toContainText('0.660');
});

test('a view over an incomplete index says it is out of date, not a raw error', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-view'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\npuppy\n');
  const setResolveError = await stubIndexWithView(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openEmbeddingViews(page);

  setResolveError('embedding_index_incomplete');
  await page.getByTestId('embedding-view-open-1').click();

  const err = page.getByTestId('embedding-view-resolve-error-1');
  await expect(err).toBeVisible();
  await expect(err).toContainText('out of date');
  await expect(err).not.toContainText('embedding_index_incomplete');
  await expect(page.getByTestId('embedding-view-results-1')).toHaveCount(0);
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();
});
