import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  openDiscoverTab,
  uniqueName,
} from './helpers';

// A saved lens is a REAL grid view.
//
// Opening a saved lens must affect the GRID, not just the sidebar list: it
// FILTERS the main grid to exactly the resolved row_ids and SORTS them by
// distance/score (ranked order), with distance/score VISIBLE in the grid path
// (result columns), a stale/incomplete resolve BLOCKING the grid open with a
// refresh-needed message, and an explicit exit affordance restoring the full
// sheet. The embeddings index + lens endpoints are stubbed; the sheet data
// route is served by the REAL backend (the row_ids ordered fetch is a real
// contract covered by a Python route test).

const INDEX_ID = 'embidx_grid';

function makeIndex(sheetId: number) {
  return {
    index_id: INDEX_ID,
    name: 'headline embeddings',
    sheet_id: sheetId,
    modality: 'text',
    provider_id: 'fastembed',
    model_id: 'paraphrase-MiniLM',
    source_columns: ['headline'],
    status: 'idle',
    total_items: 4,
    ready_items: 4,
    stale_items: 0,
    stale_source_items: 0,
    missing_source_items: 0,
    error_items: 0,
    refresh_needed: false,
    last_refreshed_at: '2026-06-21T00:00:00Z',
    remote: false,
    provider_kind: 'local_runtime',
    provider_policy: {
      allow_remote: false,
      allow_remote_automatic_refresh: false,
      max_cost_usd_per_refresh: null,
    },
    maintenance: { mode: 'manual', schedule: null },
    freshness: {
      reason: 'current',
      current: 4,
      ready: 4,
      missing: 0,
      stale: 0,
      error: 0,
      total: 4,
      refresh_needed: false,
      scope_resolved: true,
      maintenance_mode: 'manual',
      last_refreshed_at: '2026-06-21T00:00:00Z',
      last_refresh_job_id: null,
      last_refresh_receipt_id: null,
      pending_refresh_job_id: null,
    },
    space_id: 'emb_grid',
    dimension: 384,
    distance_metric: 'cosine',
  };
}

interface GridLensStubs {
  /** The ranked row-id order the resolve serves (also the grid order). */
  rankedRowIds: number[];
  setResolveError(code: string | null): void;
}

async function fetchRowIds(
  request: APIRequestContext,
  pid: string,
  sheetId: number,
): Promise<number[]> {
  const data = (await (
    await request.get(`/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=20`)
  ).json()) as { rows: Array<{ id: number }> };
  return data.rows.map((r) => r.id);
}

async function stubIndexAndLens(
  page: Page,
  pid: string,
  sheetId: number,
  rankedRowIds: number[],
  totalOverride?: number,
  maxResolveWindow?: number,
): Promise<GridLensStubs> {
  let resolveErrorCode: string | null = null;
  // a single pre-seeded lens over THIS index (the save path is covered elsewhere)
  const lens = {
    id: 1,
    name: 'Similar to row',
    sheet_id: sheetId,
    spec: {
      schema_version: 'frisket.lens.v1',
      query: {
        kind: 'embedding_similarity',
        embedding_index_id: INDEX_ID,
        anchor: { kind: 'row', row_id: rankedRowIds[0] ?? 1 },
      },
      presentation: {},
    },
    op_id: 10,
    created_at: '2026-06-21T00:00:00Z',
    updated_at: '2026-06-21T00:00:00Z',
  };

  await page.route(`**/api/projects/${pid}/embeddings/v1/indexes*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.embedding_index_list.v1',
        sheet_id: sheetId,
        indexes: [makeIndex(sheetId)],
      }),
    }),
  );

  await page.route(new RegExp(`/api/projects/${pid}/lenses(\\?[^/]*)?$`), (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([lens]),
    }),
  );

  await page.route(`**/api/projects/${pid}/lenses/*/resolve*`, (route) => {
    if (resolveErrorCode) {
      route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: {
            code: resolveErrorCode,
            message: `index not searchable (${resolveErrorCode})`,
            field: 'embedding_index_id',
          },
        }),
      });
      return;
    }
    // Honor the limit/offset window like the real query-preview route (the client
    // sends ?limit=); total stays the full hit count.
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get('limit') ?? rankedRowIds.length);
    const offset = Number(url.searchParams.get('offset') ?? 0);
    const windowLimit = maxResolveWindow === undefined
      ? limit
      : Math.min(limit, maxResolveWindow);
    const windowed = rankedRowIds.slice(
      offset,
      offset + (windowLimit > 0 ? windowLimit : 0),
    );
    const scores: Record<string, { distance: number; score: number }> = {};
    windowed.forEach((rid, i) => {
      scores[String(rid)] = { distance: 0.1 + i * 0.2, score: 0.9 - i * 0.2 };
    });
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        lens_id: 1,
        schema_version: 'frisket.query_preview.v1',
        sheet_id: sheetId,
        query: { kind: 'embedding_similarity', embedding_index_id: INDEX_ID, anchor: { kind: 'row', row_id: rankedRowIds[0] } },
        query_hash: 'sha256:lens-grid',
        row_ids: windowed,
        row_count: windowed.length,
        // total may exceed the returned window (more similar rows than the cap) — the
        // banner then honestly shows "showing N of M ranked rows".
        total: totalOverride ?? rankedRowIds.length,
        offset,
        limit,
        evaluator: 'embedding_similarity',
        scores,
      }),
    });
  });

  return {
    rankedRowIds,
    setResolveError: (code) => {
      resolveErrorCode = code;
    },
  };
}

async function openLensFromPanel(page: Page) {
  await openDiscoverTab(page, 'Embeddings');
  await expect(page.getByTestId(`embedding-index-${INDEX_ID}`)).toBeVisible();
  await expect(page.getByTestId('embedding-lens-open-1')).toBeVisible();
  await page.getByTestId('embedding-lens-open-1').click();
}

test('opening a lens shows ONLY the lens row_ids in the grid, in score order', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-lens-grid'));
  const sheetId = await importCsv(
    request,
    pid,
    'a.csv',
    'headline\ncat\nkitten\nairplane\npuppy\n',
  );
  const allIds = await fetchRowIds(request, pid, sheetId);
  expect(allIds.length).toBe(4);
  // pick a 2-row subset in a non-source order (descending row id) so the
  // ranked order is observable and distinct from the natural sheet order.
  const ranked = [allIds[3], allIds[1]];
  await stubIndexAndLens(page, pid, sheetId, ranked);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // the grid's data request must carry the ranked row_ids in order
  const lensDataResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    if (url.pathname !== `/api/projects/${pid}/sheets/${sheetId}/data`) return false;
    return url.searchParams.get('row_ids') === ranked.join(',');
  });
  await openLensFromPanel(page);
  const body = await (await lensDataResponse).json();
  // backend returns EXACTLY the lens rows, in the ranked order (not the full sheet)
  expect(body.total).toBe(2);
  expect(body.rows.map((r: { id: number }) => r.id)).toEqual(ranked);

  // an explicit lens-view banner marks the grid as a lens view
  await expect(page.getByTestId('lens-view-banner')).toBeVisible();
  await expect(page.getByTestId('lens-view-banner')).toContainText('Similar to row');
});

test('distance and score are visible in the grid path (result columns)', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-lens-grid'));
  const sheetId = await importCsv(
    request,
    pid,
    'a.csv',
    'headline\ncat\nkitten\nairplane\npuppy\n',
  );
  const allIds = await fetchRowIds(request, pid, sheetId);
  const ranked = [allIds[2], allIds[0]];
  await stubIndexAndLens(page, pid, sheetId, ranked);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openLensFromPanel(page);

  // distance/score surface as grid result columns (NOT only the sidebar list,
  // NOT raw vectors) — the canvas grid header advertises them.
  const cols = page.getByTestId('lens-view-score-columns');
  await expect(cols).toBeVisible();
  await expect(cols).toContainText('distance');
  await expect(cols).toContainText('score');
});

test('a stale/incomplete resolve blocks the grid open with refresh-needed, no partial grid', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-lens-grid'));
  const sheetId = await importCsv(
    request,
    pid,
    'a.csv',
    'headline\ncat\nkitten\nairplane\npuppy\n',
  );
  const allIds = await fetchRowIds(request, pid, sheetId);
  const ranked = [allIds[1], allIds[0]];
  const { setResolveError } = await stubIndexAndLens(page, pid, sheetId, ranked);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  setResolveError('embedding_index_incomplete');
  await openLensFromPanel(page);

  // refresh-needed surfaces in the GRID open path, not the sidebar list
  const err = page.getByTestId('lens-view-open-error');
  await expect(err).toBeVisible();
  await expect(err).toContainText('out of date');
  // the grid was NOT switched into a partial lens view
  await expect(page.getByTestId('lens-view-banner')).toHaveCount(0);
});

test('exiting the lens view restores the full sheet in the grid', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-lens-grid'));
  const sheetId = await importCsv(
    request,
    pid,
    'a.csv',
    'headline\ncat\nkitten\nairplane\npuppy\n',
  );
  const allIds = await fetchRowIds(request, pid, sheetId);
  const ranked = [allIds[3], allIds[2]];
  await stubIndexAndLens(page, pid, sheetId, ranked);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openLensFromPanel(page);
  await expect(page.getByTestId('lens-view-banner')).toBeVisible();

  // exiting clears row_ids → the grid pages the FULL sheet again
  const fullDataResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      !url.searchParams.has('row_ids')
    );
  });
  await page.getByTestId('lens-view-exit').click();
  const full = await (await fullDataResponse).json();
  expect(full.total).toBe(4);
  await expect(page.getByTestId('lens-view-banner')).toHaveCount(0);
});

test('the banner honestly shows "showing N of M" when the window is smaller than the total', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-lens-grid'));
  const sheetId = await importCsv(
    request,
    pid,
    'a.csv',
    'headline\ncat\nkitten\nairplane\npuppy\n',
  );
  const allIds = await fetchRowIds(request, pid, sheetId);
  // resolve returns a 2-row window but the index has 60 similar rows total —
  // the banner must say "showing 2 of 60 ranked rows", not just "2 ranked rows".
  const ranked = [allIds[3], allIds[1]];
  await stubIndexAndLens(page, pid, sheetId, ranked, 60);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openLensFromPanel(page);
  const banner = page.getByTestId('lens-view-banner');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText('showing 2 of 60 ranked rows');
});

test('NER initialized from a lens submits complete exact membership, not its grid window', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-lens-action-scope'));
  const sheetId = await importCsv(
    request,
    pid,
    'people.csv',
    'headline\nAda Lovelace\nGrace Hopper\nKatherine Johnson\nDorothy Vaughan\n',
  );
  const allIds = await fetchRowIds(request, pid, sheetId);
  const ranked = [allIds[3], allIds[1], allIds[2], allIds[0]];
  await stubIndexAndLens(page, pid, sheetId, ranked, ranked.length, 2);

  // This is a request-serialization test; make the intercepted local engine
  // selectable even when the optional spaCy runtime is absent on the host.
  await page.route(`**/api/projects/${pid}/actions/v1/catalog`, async (route) => {
    const response = await route.fetch();
    const body = await response.json() as {
      actions: Array<{
        kind: string;
        ui_hints?: { engines?: Array<Record<string, unknown>> };
      }>;
    };
    const ner = body.actions.find((action) => action.kind === 'map.ner');
    const spacy = ner?.ui_hints?.engines?.find((engine) => engine.id === 'spacy');
    if (spacy) {
      spacy.available = true;
      spacy.error = null;
    }
    await route.fulfill({ response, json: body });
  });

  let postedAction: Record<string, unknown> | null = null;
  let sawRun!: () => void;
  const runStarted = new Promise<void>((resolve) => {
    sawRun = resolve;
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    postedAction = route.request().postDataJSON() as Record<string, unknown>;
    sawRun();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'map.ner', action_id: 'act-lens-scope' },
        status: 'completed',
        project_id: pid,
        run_id: 9301,
        receipt_id: 'receipt-lens-scope',
        errors: [],
      }),
    });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openLensFromPanel(page);
  await expect(page.getByTestId('lens-view-banner')).toContainText(
    'showing 2 of 4 ranked rows',
  );

  await openAction(page, 'map.ner');
  await expect(page.getByTestId('run-button')).toBeEnabled();
  await page.getByTestId('run-button').click();
  await runStarted;

  expect(postedAction?.kind).toBe('map.ner');
  const rowScope = postedAction?.row_scope as {
    sheet_id?: number;
    selector?: {
      kind?: string;
      membership?: { row_ids?: number[] };
    };
  } | undefined;
  expect(rowScope?.sheet_id).toBe(sheetId);
  expect(rowScope?.selector?.kind).toBe('exact_membership');
  expect([...(rowScope?.selector?.membership?.row_ids ?? [])].sort((a, b) => a - b))
    .toEqual([...ranked].sort((a, b) => a - b));
  expect(postedAction?.schema_version).toBe('frisket.action.v2');
  expect(postedAction?.params).not.toHaveProperty('sheet_id');
  expect(postedAction?.params).not.toHaveProperty('row_ids');
});
