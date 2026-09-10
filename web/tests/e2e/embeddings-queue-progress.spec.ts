import { expect, test, type Page } from '@playwright/test';
import { embeddingIndexFixture, embeddingIndexListFixture } from '../support/embeddingIndexFixtures';
import { createProject, importCsv, openDiscoverTab, uniqueName } from './helpers';

// Embedding refreshes can take long enough to require queue progress. Creation
// is metadata-only and instant (executor/action_families/embeddings.py's
// _run_index_create writes a space + index row, no provider call); the
// actual embedding work is embedding.index_refresh, an EXISTING
// QUEUED_ACTION_JOB (executor/action_specs.py) — so the fix chains create ->
// refresh automatically (components/embeddings/useEmbeddingsPanelController.ts's
// runRefresh) instead of leaving the index in a manual "refresh needed"
// limbo, polls the launched job to completion, and reloads. The embedding
// endpoints + the job-queue endpoints are stubbed (like native-embeddings-ui
// .spec.ts) with a deliberate 'running' window so the test can observe the
// job in the jobs panel mid-flight — WorkbenchJobSplitPanel activates the
// project job resource's live dock lane, which makes that visible without any
// manual reload or feature-local polling owner.

const INDEX_ID = 'embidx_queue_progress';
const JOB_ID = 913;
const RUNNING_WINDOW_MS = 3000;

function makeIndex(sheetId: number, ready: boolean) {
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
    ready_items: ready ? 2 : 0,
    stale_items: 0,
    error_items: 0,
    refresh_needed: !ready,
    last_refreshed_at: ready ? '2026-07-08T00:00:00Z' : null,
    remote: false,
    provider_policy: {
      allow_remote: false,
      allow_remote_automatic_refresh: false,
      max_cost_usd_per_refresh: null,
    },
    space_id: 'emb_queue_progress',
    dimension: 384,
    distance_metric: 'cosine',
  });
}

function jobPayload(pid: string, status: 'running' | 'done') {
  return {
    schema_version: 'frisket.job.v1',
    project_id: pid,
    job_id: JOB_ID,
    kind: 'action.run',
    run_id: null,
    receipt_id: null,
    // ActionJob requires payload_ref; without it validateActionJob rejects the
    // job-detail response, the embeddings controller's poll (which swallows the
    // error and returns) ends instantly, and the card's busy 'Refreshing…' state
    // never becomes observable. Emit the (empty) ref so the mock validates like
    // the real job endpoint.
    payload_ref: {},
    status: status === 'done' ? 'done' : 'running',
    action_kind: 'embedding.index_refresh',
    action_name: 'Refresh embedding index',
    attempts: 1,
    max_attempts: 3,
    lease: { locked_by: null, locked_at: null, lease_expires_at: null, lease_expired: false },
    timing: { created_at: '2026-07-08T00:00:00Z', started_at: '2026-07-08T00:00:00Z', finished_at: null },
    error: null,
    result_summary: { status, index_id: INDEX_ID, refreshed: status === 'done' ? 2 : 1 },
  };
}

async function installStubs(page: Page, pid: string, sheetId: number) {
  let index: ReturnType<typeof makeIndex> | null = null;
  let refreshLaunchedAt: number | null = null;

  function jobStatus(): 'running' | 'done' {
    if (refreshLaunchedAt === null) return 'done';
    return Date.now() - refreshLaunchedAt < RUNNING_WINDOW_MS ? 'running' : 'done';
  }

  await page.route(`**/api/projects/${pid}/embeddings/v1/provider-catalog*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.embedding_provider_catalog.v1',
        modality: 'text',
        source_column_type: 'text',
        providers: [
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
        ],
      }),
    }),
  );

  await page.route(`**/api/projects/${pid}/embeddings/v1/indexes*`, (route) => {
    if (index && jobStatus() === 'done' && index.refresh_needed) {
      index = makeIndex(sheetId, true);
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(embeddingIndexListFixture(sheetId, index ? [index] : [])),
    });
  });

  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as { action_id?: string };
    if (body.action_id === 'embedding.index_create') {
      index = makeIndex(sheetId, false);
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
      refreshLaunchedAt = Date.now();
      // Real embedding.index_refresh is a QUEUED_ACTION_JOB — the v1 run
      // route returns 'queued' with a job_id rather than completing inline.
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          action: { kind: body.action_id, action_id: 'act-refresh' },
          status: 'queued',
          project_id: pid,
          run_id: null,
          job_id: JOB_ID,
          receipt_id: 'rcpt-refresh',
          outputs: [],
          errors: [],
        }),
      });
      return;
    }
    await route.continue();
  });

  await page.route(`**/api/projects/${pid}/actions/jobs/${JOB_ID}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(jobPayload(pid, jobStatus())),
    });
  });

  // Anchored (not a bare glob '*') so it matches ONLY the list endpoint
  // ('/actions/jobs' + optional query) and never the job-detail route
  // ('/actions/jobs/{id}') registered above — glob '?' matches any single
  // character, including '/', so a naive 'actions/jobs?*' pattern would also
  // swallow '/actions/jobs/913'.
  await page.route(new RegExp(`/api/projects/${pid}/actions/jobs(\\?[^/]*)?$`), async (route) => {
    const jobs = refreshLaunchedAt === null ? [] : [jobPayload(pid, jobStatus())];
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.job_list.v1',
        project_id: pid,
        jobs,
      }),
    });
  });

  return {
    isBuilding: () => refreshLaunchedAt !== null && jobStatus() === 'running',
  };
}

test('create embeddings enqueues the build, the jobs panel shows it running, and the embeddings panel refreshes to ready on completion without a manual reload', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-emb-queue-progress'));
  const sheetId = await importCsv(request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  await installStubs(page, pid, sheetId);

  await page.goto(`/p/${pid}`);
  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId('embeddings-panel')).toBeVisible();
  await page.getByTestId('embeddings-add-button').click();
  await expect(page.getByTestId('embedding-model-field')).toBeVisible();
  await page.getByTestId('embedding-model-field').click();
  await page.getByTestId('embedding-model-card-paraphrase-MiniLM').click();
  await page.getByTestId('embedding-create-next').click();
  await expect(page.getByTestId('embedding-create-summary')).toBeVisible();
  await page.getByTestId('embedding-create').click();

  // The index appears immediately; the create dialog does not block on the
  // (queued, possibly long) build that follows.
  const card = page.getByTestId(`embedding-index-${INDEX_ID}`);
  await expect(card).toBeVisible();

  // The build is chained automatically (no second manual "Refresh" click) —
  // the card's own refresh control reflects the in-flight build.
  await expect(page.getByTestId(`embedding-refresh-${INDEX_ID}`)).toHaveText(/Refreshing/);

  // The jobs panel (bottom dock, default-active tab) shows the SAME queued
  // build running, live — the project resource's active live-dock lane
  // surfaces it without any action on this page (runs-are-jobs-parity-v1's
  // "every execution path is jobs-panel visible" extended to embeddings'
  // queue path).
  const jobsTab = page.getByTestId('bottom-dock-tab-jobs');
  await expect(jobsTab).toHaveAttribute('aria-selected', 'true');
  const jobRow = page.getByTestId(`bottom-dock-job-${JOB_ID}`);
  await expect(jobRow).toBeVisible({ timeout: 10_000 });
  await expect(jobRow).toHaveAttribute('data-action-kind', 'embedding.index_refresh');

  // Terminal: the queued job completes, and — WITHOUT a manual reload — the
  // embeddings panel surface refreshes to show the index ready.
  await expect(page.getByTestId(`embedding-refresh-needed-${INDEX_ID}`)).toHaveCount(0, {
    timeout: 15_000,
  });
  await expect(page.getByTestId(`embedding-status-${INDEX_ID}`)).toContainText('2/2 ready');
  await expect(page.getByTestId(`embedding-refresh-${INDEX_ID}`)).toHaveText(/^\s*Refresh\s*$/);
});
