import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, openToolbarOverflow, uniqueName } from './helpers';

const CSV = `story
"A city auditor found duplicate invoices in the public works department."
`;

const RUN_TOTAL = 58;
const RECEIPT_TOTAL = 64;

type WireProvenanceManifest = {
  project_id: string;
  models: Array<{
    model: string;
    provider: string | null;
    runs: number;
    rows: number;
    cost: number;
  }>;
  touched: string[];
  providers: string[];
  total_cost: number;
  action_kinds: Array<{
    action_kind: string;
    action_name: string;
    runs: number;
    rows: number;
    failed_rows: number;
    cost: number;
  }>;
  runs: Array<{
    run_id: string;
    sheet_id: string;
    action_kind: string;
    action_name: string;
    model: string | null;
    provider: string | null;
    status: string;
    total_rows: number;
    completed_rows: number;
    failed_rows: number;
    cost: number;
    started_at: string | null;
    finished_at: string | null;
  }>;
  runs_page: WireProvenancePage;
  receipts: Array<{
    receipt_id: string;
    action_kind: string;
    status: string;
    run_id: string | null;
    created_at: string | null;
  }>;
  receipts_page: WireProvenancePage;
};

type WireProvenancePage = {
  schema_version: 'frisket.provenance_runs_page.v1' | 'frisket.provenance_receipts_page.v1';
  order: 'desc';
  offset: number;
  limit: number;
  total: number;
  has_more: boolean;
  next_offset: number | null;
};

function pageMeta(
  schemaVersion: WireProvenancePage['schema_version'],
  total: number,
  offset: number,
  limit: number,
): WireProvenancePage {
  const returned = Math.max(0, Math.min(limit, Math.max(0, total - offset)));
  const nextOffset = offset + returned;
  return {
    schema_version: schemaVersion,
    order: 'desc',
    offset,
    limit,
    total,
    has_more: nextOffset < total,
    next_offset: nextOffset < total ? nextOffset : null,
  };
}

function provenanceFixture(
  projectId: string,
  sheetId: number,
  runsOffset: number,
  runsLimit: number,
  receiptsOffset: number,
  receiptsLimit: number,
): WireProvenanceManifest {
  const runIndexes = Array.from({ length: RUN_TOTAL }, (_, index) => index);
  const receiptIndexes = Array.from({ length: RECEIPT_TOTAL }, (_, index) => index);
  const runs = runIndexes.slice(runsOffset, runsOffset + runsLimit).map((index) => ({
    run_id: String(9000 - index),
    sheet_id: String(sheetId),
    action_kind: index % 2 === 0 ? 'map.classify' : 'map.template',
    action_name: index % 2 === 0 ? 'Classify rows' : 'Template',
    model: index % 2 === 0 ? 'gemini/gemini-2.5-flash' : null,
    provider: index % 2 === 0 ? 'gemini' : null,
    status: 'completed',
    total_rows: 1,
    completed_rows: 1,
    failed_rows: 0,
    cost: index % 2 === 0 ? 0.02 : 0,
    started_at: new Date(Date.UTC(2026, 0, 1, 12, 0, index)).toISOString(),
    finished_at: new Date(Date.UTC(2026, 0, 1, 12, 1, index)).toISOString(),
  }));
  const receipts = receiptIndexes
    .slice(receiptsOffset, receiptsOffset + receiptsLimit)
    .map((index) => ({
      receipt_id: `receipt_nav_${String(index + 1).padStart(2, '0')}`,
      action_kind: index % 2 === 0 ? 'map.classify' : 'import.rows',
      status: 'completed',
      run_id: index % 2 === 0 ? String(9000 - index) : null,
      created_at: new Date(Date.UTC(2026, 0, 1, 13, 0, index)).toISOString(),
    }));
  return {
    project_id: projectId,
    models: [
      {
        model: 'gemini/gemini-2.5-flash',
        provider: 'gemini',
        runs: 29,
        rows: 29,
        cost: 0.58,
      },
    ],
    touched: ['gemini/gemini-2.5-flash'],
    providers: ['gemini'],
    total_cost: 0.58,
    action_kinds: [
      {
        action_kind: 'map.classify',
        action_name: 'Classify rows',
        runs: 29,
        rows: 29,
        failed_rows: 0,
        cost: 0.58,
      },
      {
        action_kind: 'map.template',
        action_name: 'Template',
        runs: 29,
        rows: 29,
        failed_rows: 0,
        cost: 0,
      },
    ],
    runs,
    runs_page: pageMeta(
      'frisket.provenance_runs_page.v1',
      RUN_TOTAL,
      runsOffset,
      runsLimit,
    ),
    receipts,
    receipts_page: pageMeta(
      'frisket.provenance_receipts_page.v1',
      RECEIPT_TOTAL,
      receiptsOffset,
      receiptsLimit,
    ),
  };
}

test('provenance drawer navigates run and receipt pages independently', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-provenance-nav'));
  const sheetId = await importCsv(request, pid, 'stories.csv', CSV);

  const provenanceRequests: string[] = [];
  let delayNextOlderRunPage = false;
  let releaseDelayedOlderRunPage: (() => void) | null = null;
  await page.route(`**/api/projects/${pid}/provenance**`, async (route) => {
    const url = new URL(route.request().url());
    const runsOffset = Number(url.searchParams.get('runs_offset') ?? 0);
    const runsLimit = Number(url.searchParams.get('runs_limit') ?? 25);
    const receiptsOffset = Number(url.searchParams.get('receipts_offset') ?? 0);
    const receiptsLimit = Number(url.searchParams.get('receipts_limit') ?? 25);
    provenanceRequests.push(url.searchParams.toString());
    if (delayNextOlderRunPage && runsOffset === 25 && receiptsOffset === 0) {
      delayNextOlderRunPage = false;
      await new Promise<void>((resolve) => {
        releaseDelayedOlderRunPage = resolve;
      });
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(
        provenanceFixture(pid, sheetId, runsOffset, runsLimit, receiptsOffset, receiptsLimit),
      ),
    });
  });

  await openProject(page, pid, sheetId);
  await openToolbarOverflow(page);
  await page.getByTestId('open-provenance-manifest').click();

  const contribution = page.getByTestId('workbench-contribution-frisket-core-panel-provenance');
  await expect(contribution).toBeVisible();
  await expect(contribution).toHaveAttribute('data-schema-version', 'frisket.workbench.panel.v1');
  await expect(contribution).toHaveAttribute('data-host', 'modalOrPeek');
  await expect(contribution).toHaveAttribute('data-mode', 'peek');
  await expect(contribution).toHaveAttribute(
    'data-runtime-component-key',
    'core.panels.ProvenanceManifest',
  );
  await expect(contribution).toHaveAttribute(
    'data-required-capabilities',
    /project\.provenance\.read.*receipt\.inspect/,
  );

  const drawer = page.getByTestId('provenance-manifest');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId('provenance-run-row')).toHaveCount(25);
  await expect(drawer.getByTestId('provenance-receipt-row')).toHaveCount(25);
  await expect(drawer.getByTestId('provenance-runs-page-range')).toContainText(
    'Showing 1-25 of 58 runs',
  );
  await expect(drawer.getByTestId('provenance-receipts-page-range')).toContainText(
    'Showing 1-25 of 64 receipts',
  );
  expect(provenanceRequests).toContain(
    'runs_offset=0&runs_limit=25&receipts_offset=0&receipts_limit=25',
  );

  provenanceRequests.length = 0;
  await drawer.getByTestId('provenance-runs-older').click();
  await expect(drawer.getByTestId('provenance-run-list')).toContainText('run 8975');
  await expect(drawer.getByTestId('provenance-runs-page-range')).toContainText(
    'Showing 26-50 of 58 runs',
  );
  await expect(drawer.getByTestId('provenance-receipts-page-range')).toContainText(
    'Showing 1-25 of 64 receipts',
  );
  expect(provenanceRequests).toContain(
    'runs_offset=25&runs_limit=25&receipts_offset=0&receipts_limit=25',
  );
  await expect(drawer.getByTestId('provenance-provider-list')).toContainText('gemini');
  await expect(drawer.getByTestId('provenance-model-table')).toContainText(
    'gemini/gemini-2.5-flash',
  );
  await expect(drawer.getByTestId('provenance-total-cost')).toContainText('$0.58');

  provenanceRequests.length = 0;
  await drawer.getByTestId('provenance-receipts-older').click();
  await expect(drawer.getByTestId('provenance-receipt-list')).toContainText('receipt_nav_26');
  await expect(drawer.getByTestId('provenance-runs-page-range')).toContainText(
    'Showing 26-50 of 58 runs',
  );
  await expect(drawer.getByTestId('provenance-receipts-page-range')).toContainText(
    'Showing 26-50 of 64 receipts',
  );
  expect(provenanceRequests).toContain(
    'runs_offset=25&runs_limit=25&receipts_offset=25&receipts_limit=25',
  );

  provenanceRequests.length = 0;
  await drawer.getByTestId('provenance-receipts-newer').click();
  await expect(drawer.getByTestId('provenance-receipt-list')).toContainText('receipt_nav_01');
  await expect(drawer.getByTestId('provenance-runs-page-range')).toContainText(
    'Showing 26-50 of 58 runs',
  );
  await expect(drawer.getByTestId('provenance-receipts-page-range')).toContainText(
    'Showing 1-25 of 64 receipts',
  );
  expect(provenanceRequests).toContain(
    'runs_offset=25&runs_limit=25&receipts_offset=0&receipts_limit=25',
  );

  provenanceRequests.length = 0;
  await drawer.getByTestId('provenance-runs-newer').click();
  await expect(drawer.getByTestId('provenance-run-list')).toContainText('run 9000');
  await expect(drawer.getByTestId('provenance-runs-page-range')).toContainText(
    'Showing 1-25 of 58 runs',
  );
  await expect(drawer.getByTestId('provenance-receipts-page-range')).toContainText(
    'Showing 1-25 of 64 receipts',
  );
  expect(provenanceRequests).toContain(
    'runs_offset=0&runs_limit=25&receipts_offset=0&receipts_limit=25',
  );

  delayNextOlderRunPage = true;
  provenanceRequests.length = 0;
  await drawer.getByTestId('provenance-runs-older').click();
  await expect(drawer.getByTestId('provenance-loading')).toBeVisible();
  await drawer.getByTestId('provenance-refresh').click();
  await expect(drawer.getByTestId('provenance-run-list')).toContainText('run 9000');
  expect(provenanceRequests).toContain(
    'runs_offset=0&runs_limit=25&receipts_offset=0&receipts_limit=25',
  );
  expect(releaseDelayedOlderRunPage).not.toBeNull();
  const delayedOlderResponse = page.waitForResponse((response) => {
    const responseUrl = new URL(response.url());
    return (
      responseUrl.pathname.endsWith(`/api/projects/${pid}/provenance`) &&
      responseUrl.searchParams.get('runs_offset') === '25' &&
      responseUrl.searchParams.get('receipts_offset') === '0'
    );
  });
  releaseDelayedOlderRunPage!();
  await delayedOlderResponse;
  await expect(drawer.getByTestId('provenance-runs-page-range')).toContainText(
    'Showing 1-25 of 58 runs',
  );

  const href = await drawer.getByTestId('provenance-download-json').getAttribute('href');
  expect(href).toMatch(/^data:application\/json/);
  const encoded = href!.slice(href!.indexOf(',') + 1);
  const exported = JSON.parse(decodeURIComponent(encoded));
  expect(exported.manifest.runs_page).toMatchObject({
    offset: 0,
    limit: 25,
    total: RUN_TOTAL,
    has_more: true,
    next_offset: 25,
  });
  expect(exported.manifest.receipts_page).toMatchObject({
    offset: 0,
    limit: 25,
    total: RECEIPT_TOTAL,
    has_more: true,
    next_offset: 25,
  });
  expect(JSON.stringify(exported)).not.toMatch(/recipe/i);
});
