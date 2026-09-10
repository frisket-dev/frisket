import { expect, test } from '@playwright/test';
import { routeV1ActionRun, routeV1ActionStatus } from './actionStatusFixtures';
import {
  createProject,
  dblclickCell,
  importCsv,
  openAction,
  openProject,
  runAndWait,
  seedGeoSheet,
  sheetColumns,
  uniqueName,
} from './helpers';

// Four run-failure trust bugs. Failures happen —
// that's fine — but they must be VISIBLE and EXPLAINED. Each test pins one
// report; the RED state is the pre-fix behaviour named in the assertion.

// --- Report 1: a failing run must never show a message-less red toast --------

test('a failing census run surfaces a non-empty error toast (never an empty red box)', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoSheet(page, {
    namePrefix: 'censusfix',
    filename: 'rows.csv',
    csv: 'address,point\n"1600 Pennsylvania Ave NW",\n',
  });
  // The action endpoint fails with an errors[] entry whose message is BLANK —
  // exactly the shape that used to collapse to a message-less red toast.
  await page.request.post(`/api/projects/${pid}/secrets`, {
    data: { name: 'CENSUS_API_KEY', value: 'e2e-fake-key' },
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'enrich.census_demographics', action_id: 'act-x' },
        status: 'failed',
        project_id: pid,
        run_id: null,
        receipt_id: null,
        outputs: [],
        warnings: [],
        errors: [{ message: '' }],
      }),
    });
  });

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await openAction(page, 'enrich.census_demographics');
  await expect(page.getByTestId('generated-action-form')).toBeVisible();
  await expect(page.getByRole('combobox', { name: 'Geo point column' })).toHaveValue('point');
  await page.getByTestId('generated-action-run').click();

  const toast = page.getByTestId('error-toast');
  await expect(toast).toBeVisible();
  // The regression: an empty red box. The fix guarantees SOME message text.
  await expect(toast).not.toBeEmpty();
  expect((await toast.innerText()).trim().length).toBeGreaterThan(0);
});

test('a failing run with a real error message surfaces that message verbatim', async ({ page }) => {
  const { pid, sheetId } = await seedGeoSheet(page, {
    namePrefix: 'censusfix',
    filename: 'rows.csv',
    csv: 'address,point\n"1600 Pennsylvania Ave NW",\n',
  });
  await page.request.post(`/api/projects/${pid}/secrets`, {
    data: { name: 'CENSUS_API_KEY', value: 'e2e-fake-key' },
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'enrich.census_demographics', action_id: 'act-x' },
        status: 'failed',
        project_id: pid,
        run_id: null,
        receipt_id: null,
        outputs: [],
        warnings: [],
        errors: [{ message: 'Missing Census API key (set CENSUS_API_KEY)' }],
      }),
    });
  });

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await openAction(page, 'enrich.census_demographics');
  await page.getByTestId('generated-action-run').click();

  await expect(page.getByTestId('error-toast')).toContainText('Missing Census API key');
});

// --- Report 3: a disabled Run must state WHY, and offer the honest overwrite --

test('census without a geo_point source disables Run and states the reason', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('censusfix-noreason'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'address\n"123 Main St"\n');

  await openProject(page, pid, sheetId);
  await openAction(page, 'enrich.census_demographics');
  await expect(page.getByTestId('generated-action-form')).toBeVisible();

  const runButton = page.getByTestId('generated-action-run');
  await expect(runButton).toBeDisabled();
  // The fix: a visible hint (and a matching tooltip) states the reason.
  const reason = page.getByTestId('census-no-geo-point');
  await expect(reason).toBeVisible();
  await expect(reason).toContainText(/geo_point/i);
  await expect(runButton).toHaveAttribute('title', /required fields|CENSUS_API_KEY|geo_point/i);
});

test('a NEW-column collision with an AI column offers an overwrite path, not a dead end', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('censusfix-collide'));
  const sheetId = await importCsv(page.request, pid, 'notes.csv', 'note\nalpha\nbeta\n');
  // Create a real AI-generated column named "demo_population" (deterministic
  // template). Typing "demo population" (space) is NOT an exact match, so the
  // destination stays "New column" and trips the normalized collision guard.
  await runAndWait(page.request, pid, {
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { template: { text: '{{note}}!' } },
    output_names: { rendered: 'demo_population' },
    idempotency_key: `failure-surfacing-template-collision-${pid}`,
  });

  await openProject(page, pid, sheetId);
  // Use a legacy-form action here because registered typed actions reject
  // existing output names instead of offering the legacy overwrite escape hatch.
  await openAction(page, 'map.classify');
  await expect(page.getByTestId('action-form')).toBeVisible();

  // Type a name that normalizes onto the existing AI column → output-name
  // collision while the destination is still "New column".
  const newColInput = page.getByTestId('new-column-name');
  await newColInput.click();
  await newColInput.fill('demo population');

  const runButton = page.getByTestId('run-button');
  await expect(runButton).toBeDisabled();
  const reason = page.getByTestId('run-disabled-reason');
  await expect(reason).toContainText(/already exists/i);

  // The honest path: overwrite the existing AI column (re-target it). It is NOT
  // auto-applied — the user clicks it.
  const overwrite = page.getByTestId('run-overwrite-existing');
  await expect(overwrite).toBeVisible();
  await overwrite.click();

  // The collision clears (destination is now the existing column).
  await expect(page.getByTestId('run-disabled-reason')).not.toContainText(/already exists/i);
  await expect(page.getByTestId('run-overwrite-existing')).toHaveCount(0);
});

// --- Report 4: an in-memory preview cell error is readable in full -----------

test('clicking a preview error cell shows the full error text in the Detail panel', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('censusfix-preview'));
  // Short cell content keeps every column at its default width so the shared
  // cell-click helper's geometry is exact. Row 0 makes the local program fail
  // with a stable, readable error while row 1 succeeds.
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'note\n"fail"\n"beta"\n');
  // A real AI column the preview OVERWRITES in memory (via the overwrite path),
  // so we click a stable, testid-addressable column instead of a virtual one.
  await runAndWait(page.request, pid, {
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { template: { text: '{{note}}' } },
    output_names: { rendered: 'matched_out' },
    idempotency_key: `failure-surfacing-template-preview-${pid}`,
  });
  const columns = await sheetColumns(page.request, pid, sheetId);

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  await openAction(page, 'map.python');
  await expect(page.getByTestId('field-code')).toBeVisible();
  await page.getByTestId('field-code').fill(
    'if row["note"] == "fail":\n    raise ValueError("preview row timed out")\nresult = row["note"]',
  );
  await page.getByText('Return schema and output routes', { exact: true }).click();
  await page.getByTestId('field-return_schema').fill('{"type":"string"}');
  await page.getByTestId('field-output_routes').fill(JSON.stringify([
    { name: 'computed', path: '$', target: { kind: 'column', type: 'text' } },
  ]));
  // Preview into the existing compatible generated column.
  const dest = page.getByTestId('field-output-computed');
  await dest.click();
  await dest.fill('matched_out');
  await page.getByTestId('generated-action-preview').click();
  await expect(page.getByTestId('preview-tab-banner')).toBeVisible();
  await expect(page.getByTestId('preview-tab-stats')).toContainText(/Preview ·/, { timeout: 20_000 });

  // Dismiss the action drawer overlay (the preview overlay survives it), then
  // open the overwritten error cell's Detail via the shared helper.
  await page.getByTestId('action-drawer-close').click();
  await dblclickCell(page, columns, 'matched_out', 0);

  const previewError = page.getByTestId('inspect-detail-preview-error');
  await expect(previewError).toBeVisible();
  await expect(previewError).toContainText(/timed out/i);
});

// --- Report 2: a failed DIRECT run must be visible in the Errors dock --------

test('a failed direct run surfaces in the Errors tab (not only in History)', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('censusfix-errors'));
  const sheetId = await importCsv(request, pid, 'rows.csv', 'note\nalpha\nbeta\n');

  const runId = 8811;
  // Direct run: the v1 endpoint returns a run_id synchronously (no job row).
  await routeV1ActionRun(page, pid, {
    actionKind: 'map.python',
    receiptId: `receipt-${runId}`,
    runId,
    status: 'completed',
  });
  // The run then terminates FAILED with a run-level error message.
  await routeV1ActionStatus(page, pid, {
    actionKind: 'map.python',
    actionName: 'Python',
    projectId: pid,
    runId,
    status: 'failed',
    total: 2,
    completed: 0,
    failed: 2,
    error: 'census ACS request failed 403: Missing Census API key',
  });

  await openProject(page, pid, sheetId);
  await openAction(page, 'map.python');
  await expect(page.getByTestId('python-code-editor')).toBeVisible();
  await page.getByTestId('field-code').fill("result = row.get('note', '')");
  await page.getByTestId('generated-action-run').click();

  // The failed direct run must appear in Errors (it lives only in the run
  // controller's `run` state — never a job-queue row — so the Errors dock, fed
  // by the queue, used to miss it entirely).
  await page.getByTestId('bottom-dock-tab-errors').click();
  const panel = page.getByTestId('bottom-dock-panel');
  await expect(panel).toHaveAttribute('data-active-contribution-id', 'frisket.core.panel.errors');
  await expect(panel).toContainText('Python', { timeout: 15_000 });
  await expect(panel).toContainText('Missing Census API key');
  // And the Errors tab badge counts it.
  await expect(page.getByTestId('bottom-dock-tab-badge-errors')).toBeVisible();
});
