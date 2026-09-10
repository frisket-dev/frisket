import { expect, test, type Page } from '@playwright/test';
import { embeddingIndexFixture, embeddingIndexListFixture } from '../support/embeddingIndexFixtures';
import { createProject, importCsv, openDiscoverTab, uniqueName } from './helpers';

// Analysis actions product UI. The embedding endpoints are stubbed (like
// native-embeddings-ui.spec.ts) so the spec is deterministic regardless of whether
// a local embedder is installed. The /actions/v1/run stub returns the REAL action
// result wire shape — both index_project and index_cluster carry the child sheet id
// on the output (sheet_id + ref.sheet_id) so the UI can OPEN the child sheet.

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
    last_refreshed_at: '2026-06-20T12:00:00Z',
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

interface RunCapture {
  action_id?: string;
  sheet_name?: string;
  scope?: { kind: string };
  output_names?: Record<string, string>;
  params?: Record<string, unknown>;
}

interface Stubs {
  /** The last /actions/v1/run analysis call we captured (project/cluster). */
  lastAnalysisRun(): RunCapture | null;
  /** Force the next analysis run to fail with an insufficient-rows error. */
  failNextWithInsufficientRows(value: boolean): void;
}

async function installEmbeddingStubs(
  page: Page,
  pid: string,
  sheetId: number,
  childSheetId: number,
): Promise<Stubs> {
  const index = makeIndex(sheetId);
  let lastAnalysis: RunCapture | null = null;
  let insufficient = false;

  await page.route(`**/api/projects/${pid}/embeddings/v1/indexes*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(embeddingIndexListFixture(sheetId, [index])),
    });
  });

  await page.route(`**/api/projects/${pid}/embeddings/v1/provider-catalog*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.embedding_provider_catalog.v1',
        modality: 'text',
        source_column_type: null,
        providers: [],
      }),
    });
  });

  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as RunCapture;
    const kind = body.action_id;
    if (kind === 'embedding.index_project' || kind === 'embedding.index_cluster') {
      lastAnalysis = body;
      if (insufficient) {
        insufficient = false;
        // The real backend wire for a typed action failure: the /actions/v1/run route
        // wraps a non-completed result in HTTP 400 (_v1_action_result_http_status) with
        // the action_result envelope (status:'failed' + typed errors[].code) as the body
        // — so parseJsonResponse takes the 400 branch and throws ApiError(code).
        await route.fulfill({
          status: 400,
          contentType: 'application/json',
          body: JSON.stringify({
            schema_version: 'frisket.action_result.v1',
            action: { kind, action_id: 'act-analysis' },
            status: 'failed',
            project_id: pid,
            run_id: null,
            receipt_id: null,
            outputs: [],
            errors: [
              {
                code: 'embedding_analysis_insufficient_rows',
                message: 'need at least k embedded rows to cluster',
              },
            ],
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          action: { kind, action_id: 'act-analysis' },
          status: 'completed',
          project_id: pid,
          run_id: null,
          receipt_id: 'rcpt-analysis',
          outputs: [
            {
              kind: 'materialized_sheet',
              name: 'analysis sheet',
              sheet_id: childSheetId,
              ref: { sheet_id: childSheetId },
            },
          ],
          errors: [],
        }),
      });
      return;
    }
    await route.continue();
  });

  return {
    lastAnalysisRun: () => lastAnalysis,
    failNextWithInsufficientRows: (value: boolean) => {
      insufficient = value;
    },
  };
}

async function setup(page: Page) {
  const pid = await createProject(page.request, uniqueName('e2e-emb-analysis'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  // A second REAL sheet stands in for the analysis child sheet, so the UI can
  // navigate to it and the sheet tab actually appears + activates.
  const childSheetId = await importCsv(page.request, pid, 'child.csv', 'x\n1\n2\n');
  const stubs = await installEmbeddingStubs(page, pid, sheetId, childSheetId);
  await page.goto(`/p/${pid}`);
  // Embeddings re-homed into the Discover panel (workbench-ia-right-edge-v1).
  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId('embeddings-panel')).toBeVisible();
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();
  return { pid, sheetId, childSheetId, stubs };
}

test('Run PCA posts typed params with sheet_name in the request envelope', async ({ page }) => {
  const { stubs } = await setup(page);

  await page.getByTestId(`embedding-run-pca-${INDEX_ID}`).click();

  // The run posts through the v1 action runner and navigates away — wait for the
  // capture rather than the (now-unmounted) button.
  await expect.poll(() => stubs.lastAnalysisRun()?.action_id).toBe('embedding.index_project');
  const params = stubs.lastAnalysisRun()?.params ?? {};
  expect(params.index_id).toBe(INDEX_ID);
  expect(params).not.toHaveProperty('target_sheet_name');
  expect(stubs.lastAnalysisRun()).toMatchObject({
    scope: { kind: 'project' },
    sheet_name: 'PCA of headline embeddings',
    output_names: {},
  });
});

test('Run k-means posts embedding.index_cluster with the explicit k + seed', async ({ page }) => {
  const { stubs } = await setup(page);

  await page.getByTestId(`embedding-cluster-k-${INDEX_ID}`).fill('4');
  await page.getByTestId(`embedding-cluster-seed-${INDEX_ID}`).fill('7');
  await page.getByTestId(`embedding-run-cluster-${INDEX_ID}`).click();

  await expect.poll(() => stubs.lastAnalysisRun()?.action_id).toBe('embedding.index_cluster');
  const params = stubs.lastAnalysisRun()?.params ?? {};
  expect(params.index_id).toBe(INDEX_ID);
  expect(params.k).toBe(4);
  expect(params.seed).toBe(7);
  expect(params.method).toBe('kmeans');
  expect(params).not.toHaveProperty('target_sheet_name');
  expect(stubs.lastAnalysisRun()?.sheet_name).toBe('Clusters of headline embeddings');
});

test('an insufficient-rows error shows a friendly message and does not crash', async ({ page }) => {
  const { stubs } = await setup(page);
  stubs.failNextWithInsufficientRows(true);

  await page.getByTestId(`embedding-cluster-k-${INDEX_ID}`).fill('2');
  await page.getByTestId(`embedding-run-cluster-${INDEX_ID}`).click();

  const err = page.getByTestId(`embedding-analysis-error-${INDEX_ID}`);
  await expect(err).toBeVisible();
  await expect(err).toContainText('enough');
  // The card is still mounted (no navigation, no crash).
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();
  await expect(page.getByTestId(`embedding-run-cluster-${INDEX_ID}`)).toBeEnabled();
});

test('a successful run navigates to the child sheet', async ({ page }) => {
  const { childSheetId } = await setup(page);

  await page.getByTestId(`embedding-run-pca-${INDEX_ID}`).click();

  // The grid/sheet view switched to the analysis child sheet.
  await expect(page.getByTestId(`workbench-mainView-tab-${childSheetId}`)).toHaveClass(/active/);
});
