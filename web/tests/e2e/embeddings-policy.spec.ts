import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openDiscoverTab, uniqueName } from './helpers';

// The index Details/Policy surface edits maintenance + provider
// policy by POSTing the existing embedding.index_update_policy action, shows the
// freshness/refresh-job read model, and surfaces the remote/cost gate WITHOUT
// bypassing it. Endpoints are stubbed (fastembed is not installed).

const INDEX_ID = 'embidx_pol';

function makeIndex(sheetId: number, over: Record<string, unknown> = {}) {
  return {
    index_id: INDEX_ID,
    name: 'headline embeddings',
    sheet_id: sheetId,
    modality: 'text',
    provider_id: 'openai',
    model_id: 'text-embedding-3-small',
    source_columns: ['headline'],
    status: 'idle',
    total_items: 3,
    ready_items: 2,
    stale_items: 0,
    missing_source_items: 1,
    error_items: 0,
    refresh_needed: true,
    last_refreshed_at: '2026-06-21T00:00:00Z',
    remote: true,
    provider_kind: 'platform_api',
    provider_policy: {
      allow_remote: true,
      allow_remote_automatic_refresh: false,
      max_cost_usd_per_refresh: null,
    },
    maintenance: { mode: 'manual', schedule: null },
    freshness: {
      reason: 'missing_rows',
      current: 2,
      missing: 1,
      stale: 0,
      error: 0,
      total: 3,
      last_refresh_job_id: 7,
      last_refresh_receipt_id: 'receipt_x',
      pending_refresh_job_id: null,
    },
    space_id: 'emb_pol',
    dimension: 1536,
    distance_metric: 'cosine',
    ...over,
  };
}

async function stub(
  page: Page,
  pid: string,
  sheetId: number,
  initial: Record<string, unknown> = {},
) {
  const policyPosts: Record<string, unknown>[] = [];
  let index = makeIndex(sheetId, initial);

  await page.route(`**/api/projects/${pid}/embeddings/v1/indexes*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.embedding_index_list.v1',
        sheet_id: sheetId,
        indexes: [index],
      }),
    }),
  );
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as {
      action_id?: string;
      params?: { maintenance_policy?: Record<string, unknown>; provider_policy?: Record<string, unknown> };
    };
    if (body.action_id === 'embedding.index_update_policy') {
      policyPosts.push(body);
      const p = body.params ?? {};
      index = makeIndex(sheetId, {
        maintenance: p.maintenance_policy ?? index.maintenance,
        provider_policy: p.provider_policy ?? index.provider_policy,
      });
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
          outputs: [
            { kind: 'embedding_index_update_policy', name: INDEX_ID, ref: { index_id: INDEX_ID } },
          ],
          errors: [],
        }),
      });
      return;
    }
    await route.continue();
  });
  return { policyPosts };
}

async function openPolicy(page: Page) {
  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();
  await page.getByTestId(`embedding-policy-toggle-${INDEX_ID}`).click();
  await expect(page.getByTestId(`embedding-policy-${INDEX_ID}`)).toBeVisible();
}

test('policy detail shows freshness reason + counts + refresh job + maintenance mode', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-pol'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  await stub(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openPolicy(page);

  await expect(page.getByTestId(`embedding-freshness-${INDEX_ID}`)).toContainText('missing_rows');
  await expect(page.getByTestId(`embedding-freshness-${INDEX_ID}`)).toContainText('1 missing');
  await expect(page.getByTestId(`embedding-refresh-jobs-${INDEX_ID}`)).toContainText('last refreshed');
  await expect(page.getByTestId(`embedding-policy-mode-${INDEX_ID}`)).toHaveValue('manual');
});

test('editing maintenance to scheduled round-trips through embedding.index_update_policy', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-pol'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const { policyPosts } = await stub(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openPolicy(page);

  await page.getByTestId(`embedding-policy-mode-${INDEX_ID}`).selectOption('scheduled');
  await page.getByTestId(`embedding-policy-schedule-${INDEX_ID}`).fill('@hourly');
  await page.getByTestId(`embedding-policy-save-${INDEX_ID}`).click();

  await expect.poll(() => policyPosts.length).toBe(1);
  const params = policyPosts[0].params as { maintenance_policy: { mode: string; schedule: string } };
  expect(params.maintenance_policy.mode).toBe('scheduled');
  expect(params.maintenance_policy.schedule).toBe('@hourly');
  // after reload the detail reflects the saved mode
  await expect(page.getByTestId(`embedding-policy-mode-${INDEX_ID}`)).toHaveValue('scheduled');
});

test('remote automatic refresh without a cost cap shows the gate note but still saves', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-pol'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const { policyPosts } = await stub(page, pid, sheetId);
  await page.goto(`/p/${pid}`);
  await openPolicy(page);

  // allow_remote is already on; enable automatic refresh, leave cost empty
  await page.getByTestId(`embedding-policy-allow-auto-${INDEX_ID}`).check();
  await expect(page.getByTestId(`embedding-policy-gate-note-${INDEX_ID}`)).toContainText(
    'embedding_cost_requires_confirmation',
  );
  // saving still stores the preauthorization (the POST goes through)
  await page.getByTestId(`embedding-policy-save-${INDEX_ID}`).click();
  await expect.poll(() => policyPosts.length).toBe(1);
  const params = policyPosts[0].params as {
    provider_policy: { allow_remote_automatic_refresh: boolean; max_cost_usd_per_refresh: number | null };
  };
  expect(params.provider_policy.allow_remote_automatic_refresh).toBe(true);
  expect(params.provider_policy.max_cost_usd_per_refresh).toBeNull();
});

test('corrupt policy is repaired safely with a wholesale normalized save', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-emb-pol'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'headline\ncat\nkitten\n');
  const { policyPosts } = await stub(page, pid, sheetId, {
    provider_policy: {
      allow_remote: 'false',
      allow_remote_automatic_refresh: 1,
      max_cost_usd_per_refresh: 'unbounded',
      ignored: true,
    },
    maintenance: { mode: 'scheduled', schedule: 12, ignored: true },
  });
  await page.goto(`/p/${pid}`);
  await openPolicy(page);

  await expect(page.getByTestId(`embedding-policy-repair-note-${INDEX_ID}`)).toBeVisible();
  await expect(page.getByTestId(`embedding-policy-mode-${INDEX_ID}`)).toHaveValue('manual');
  await expect(page.getByTestId(`embedding-policy-allow-remote-${INDEX_ID}`)).not.toBeChecked();
  await page.getByTestId(`embedding-policy-save-${INDEX_ID}`).click();

  await expect.poll(() => policyPosts.length).toBe(1);
  const params = policyPosts[0].params as {
    maintenance_policy: Record<string, unknown>;
    provider_policy: Record<string, unknown>;
  };
  expect(params.maintenance_policy).toEqual({ mode: 'manual' });
  expect(params.provider_policy).toEqual({
    allow_remote: false,
    allow_remote_automatic_refresh: false,
    max_cost_usd_per_refresh: null,
  });
});
