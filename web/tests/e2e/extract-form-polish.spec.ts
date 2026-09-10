import { expect, test, type Page } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  importCsv,
  openAction,
  uniqueName,
} from './helpers';

// Extract action-form contracts:
//  1. "Additional prompt instructions" is genuinely optional end-to-end
//     (was enforced both by the form's Run-gate and by the backend's
//     MapExtractParams.instruction min_length=1 + a blank-rejecting
//     validator — frisket/contracts/actions/schemas/maps.py).
//  2. The Ground-evidence + Require-citations checkbox pair collapses into
//     one three-state citation_mode select (house .row-height-select
//     style, ActionPanel.tsx's ParamInput citation_mode branch), living in
//     the main form body, mapping onto the SAME backend
//     grounding/evidence_policy params.
//  0. LIVE BLOCKING BUG found while building this: turning grounding on
//     400s the launch ("map.extract params did not validate") because
//     v1Spec.ts's extract schema-gated leg sent evidence_policy.unsupported_fields, a key
//     MapExtractEvidencePolicy (extra="forbid") does not declare. Fixed by
//     dropping that key — the withhold/warn behavior is driven purely by
//     citation_required server-side.
//  3. 'Document columns' relabeled 'Ground citations against' (copy only,
//     param name unchanged).
//  4. A run-launch validation 400 ("<kind> params did not validate") now
//     renders its per-field breakdown in the error toast
//     (errors/remediation.ts fieldErrorsFromDetails), not just the summary.
//  5. The run split-button menu's "Re-run failed/missing rows" item
//     (icon + text) now left-aligns consistently with Run all/Run selected
//     (plain text) — was rendering visibly center-aligned.
//  6. "Include confidence" / "Include justification" defaulted ON for every
//     LLM action; extract + classify's forms now default both OFF for a
//     FRESH launch (ActionPanel.tsx's initialActionFormState) — the backend
//     params (MapExtractParams.include_confidence, MapClassifyParams.
//     include_confidence/include_justification) already defaulted False, so
//     this was purely the form always sending true. A reopened saved/
//     proposed spec keeps its own recorded value regardless of kind.

type PostedExtractAction = {
  kind: string;
  params: {
    instruction?: string;
    grounding?: Record<string, unknown>;
    evidence_policy?: Record<string, unknown>;
    fields?: Array<Record<string, unknown>>;
  };
};

async function mockV1ExtractRun(page: Page, pid: string, runId = 9701): Promise<void> {
  await page.route(`**/api/projects/${pid}/actions/v1/estimate`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_estimate_result.v1',
        action: { kind: 'map.extract', action_id: 'estimate-extract-form-polish' },
        project_id: pid,
        estimate: {
          rows: 2,
          cost: 0.02,
          cost_source: 'estimated',
          billed_cost: 20_000,
          policy_id: 'frisket.pricing.identity.v1',
        },
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'map.extract', action_id: 'extract-form-polish' },
        status: 'completed',
        project_id: pid,
        run_id: runId,
        op_ids: [],
        outputs: [],
        warnings: [],
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/${runId}/status`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: runId,
        live: false,
        status: 'completed',
        done: true,
        completed_rows: 2,
        total_rows: 2,
        failed_rows: 0,
      }),
    });
  });
}

async function seedExtractProject(page: Page, namePrefix: string): Promise<{ pid: string; sheetId: number }> {
  const pid = await createProject(page.request, uniqueName(namePrefix));
  const sheetId = await importCsv(
    page.request,
    pid,
    'meetings.csv',
    [
      'title,notes',
      '"Budget hearing","Officials discussed vendor bids and transit funds."',
      '"Safety briefing","The police chief named two road closures."',
    ].join('\n'),
  );
  return { pid, sheetId };
}

async function expectAdditionalPromptAfterFields(page: Page): Promise<void> {
  await expect(page.getByLabel('Additional prompt instructions')).toHaveAttribute(
    'data-testid',
    'action-prompt',
  );
  const formOrder = await page
    .locator('.fields-builder, [data-testid="action-prompt"]')
    .evaluateAll((elements) =>
      elements.map((element) =>
        element.classList.contains('fields-builder') ? 'fields-builder' : 'action-prompt',
      ),
    );
  expect(formOrder).toEqual(['fields-builder', 'action-prompt']);
}

test('extract runs clean with fields but no instructions (item 1)', async ({ page }) => {
  const { pid, sheetId } = await seedExtractProject(page, 'e2e-extract-optional-prompt');
  await mockV1ExtractRun(page, pid);
  await page.goto(`/p/${pid}/s/${sheetId}`);

  await openAction(page, 'map.extract');
  await expectAdditionalPromptAfterFields(page);
  // Clear the field to prove it is genuinely optional end to end.
  await page.getByTestId('action-prompt').fill('');
  await expect(page.getByTestId('run-button')).toBeEnabled();
  await expect(page.getByTestId('run-disabled-reason')).toHaveCount(0);

  const runRequest = page.waitForRequest(
    (request) =>
      request.url().includes(`/api/projects/${pid}/actions/v1/run`) && request.method() === 'POST',
  );
  await clickRunButton(page);
  const posted = (await runRequest).postDataJSON() as PostedExtractAction;
  expect(posted.kind).toBe('map.extract');
  expect(posted.params.instruction).toBe('');
});

test('citation_mode default (Cite sources) grounds evidence without requiring citations', async ({
  page,
}) => {
  const { pid, sheetId } = await seedExtractProject(page, 'e2e-extract-citation-default');
  await mockV1ExtractRun(page, pid);
  await page.goto(`/p/${pid}/s/${sheetId}`);

  await openAction(page, 'map.extract');
  // The select lives in the main form body — no Advanced disclosure to open.
  await expect(page.getByTestId('field-citation_mode')).toBeVisible();
  await expect(page.getByTestId('field-citation_mode')).toHaveValue('cite');

  const runRequest = page.waitForRequest(
    (request) =>
      request.url().includes(`/api/projects/${pid}/actions/v1/run`) && request.method() === 'POST',
  );
  await clickRunButton(page);
  const posted = (await runRequest).postDataJSON() as PostedExtractAction;
  expect(posted.params.grounding).toMatchObject({ enabled: true, citation_required: false });
  expect(posted.params.evidence_policy).toMatchObject({ citation_required: false });
});

test("citation_mode 'Off' sends no grounding params", async ({ page }) => {
  const { pid, sheetId } = await seedExtractProject(page, 'e2e-extract-citation-none');
  await mockV1ExtractRun(page, pid);
  await page.goto(`/p/${pid}/s/${sheetId}`);

  await openAction(page, 'map.extract');
  await page.getByTestId('field-citation_mode').selectOption('none');

  const runRequest = page.waitForRequest(
    (request) =>
      request.url().includes(`/api/projects/${pid}/actions/v1/run`) && request.method() === 'POST',
  );
  await clickRunButton(page);
  const posted = (await runRequest).postDataJSON() as PostedExtractAction;
  expect(posted.params.grounding).toBeUndefined();
  expect(posted.params.evidence_policy).toBeUndefined();
});

test("citation_mode 'Require citations' grounds + requires, launches clean (item 0 regression)", async ({
  page,
}) => {
  const { pid, sheetId } = await seedExtractProject(page, 'e2e-extract-citation-require');
  await mockV1ExtractRun(page, pid);
  await page.goto(`/p/${pid}/s/${sheetId}`);

  await openAction(page, 'map.extract');
  await page.getByTestId('field-citation_mode').selectOption('require');
  // 'Check citations against columns' (renamed from 'Ground citations
  // against', itself a rename of 'Document columns') stays in Advanced options.
  await page.getByTestId('advanced-params-extract').locator('summary').click();
  await expect(page.getByText('Check citations against columns')).toBeVisible();
  await expect(page.getByText('Ground citations against')).toHaveCount(0);
  await expect(page.getByText('Document columns')).toHaveCount(0);
  await page.getByTestId('field-source_document_columns').fill('notes');

  const runRequest = page.waitForRequest(
    (request) =>
      request.url().includes(`/api/projects/${pid}/actions/v1/run`) && request.method() === 'POST',
  );
  await clickRunButton(page);
  const posted = (await runRequest).postDataJSON() as PostedExtractAction;
  expect(posted.params.grounding).toMatchObject({
    enabled: true,
    citation_required: true,
    source_document_columns: ['notes'],
  });
  expect(posted.params.evidence_policy).toMatchObject({ citation_required: true });
  // Item 0: MapExtractEvidencePolicy is extra="forbid" and declares ONLY
  // citation_required — this key 400'd every grounding-on launch live.
  expect(posted.params.evidence_policy).not.toHaveProperty('unsupported_fields');
  // The Run POST actually completing (no error toast) is the "launches
  // clean" half of the regression: the mocked backend always 200s the
  // shape it's given, so the payload-shape assertion above is what
  // actually guards frisket's real extra="forbid" model.
  await expect(page.getByTestId('error-toast')).toHaveCount(0);
});

test('a params-did-not-validate 400 shows which field failed, not just the summary (item 4)', async ({
  page,
}) => {
  const { pid, sheetId } = await seedExtractProject(page, 'e2e-extract-validation-details');
  await page.route(`**/api/projects/${pid}/actions/v1/estimate`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_estimate_result.v1',
        action: { kind: 'map.extract', action_id: 'estimate-extract-form-polish-error' },
        project_id: pid,
        estimate: {
          rows: 2,
          cost: 0.02,
          cost_source: 'estimated',
          billed_cost: 20_000,
          policy_id: 'frisket.pricing.identity.v1',
        },
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    await route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'map.extract', action_id: 'extract-form-polish-error' },
        status: 'error',
        project_id: pid,
        errors: [
          {
            code: 'invalid_params',
            message: 'map.extract params did not validate',
            field: null,
            details: {
              errors: [
                {
                  loc: ['fields', 0, 'type'],
                  msg: 'Value error, invalid_extract_field',
                  type: 'value_error',
                },
              ],
            },
          },
        ],
      }),
    });
  });
  await page.goto(`/p/${pid}/s/${sheetId}`);

  await openAction(page, 'map.extract');
  await clickRunButton(page);

  const toast = page.getByTestId('error-toast');
  await expect(toast).toBeVisible();
  await expect(toast).toContainText('map.extract params did not validate');
  const details = page.getByTestId('error-toast-details');
  await expect(details).toBeVisible();
  await expect(details).toContainText('fields.0.type');
  await expect(details).toContainText('invalid_extract_field');
});

test('run split-button menu items left-align consistently (item 5)', async ({ page }) => {
  const { pid, sheetId } = await seedExtractProject(page, 'e2e-extract-menu-align');
  await page.goto(`/p/${pid}/s/${sheetId}`);

  await openAction(page, 'map.extract');
  await page.getByTestId('run-scope-menu-button').click();
  const menu = page.getByTestId('run-scope-menu');
  await expect(menu).toBeVisible();

  const testIds = ['row-scope-all', 'row-scope-selected', 'row-scope-backfill'];
  const lefts: number[] = [];
  for (const testId of testIds) {
    const label = menu.getByTestId(testId).locator('> span').first();
    const box = await label.boundingBox();
    if (!box) throw new Error(`${testId} label span not visible`);
    lefts.push(box.x);
  }
  for (const left of lefts.slice(1)) {
    expect(Math.abs(left - lefts[0])).toBeLessThanOrEqual(1);
  }
});

test('a fresh extract launch carries include_confidence false unless toggled (item 6)', async ({
  page,
}) => {
  const { pid, sheetId } = await seedExtractProject(page, 'e2e-extract-confidence-default');
  await mockV1ExtractRun(page, pid);
  await page.goto(`/p/${pid}/s/${sheetId}`);

  await openAction(page, 'map.extract');
  await page.getByTestId('advanced-run-options-toggle').click();
  await expect(page.getByLabel('Include confidence')).not.toBeChecked();
  // Extract's
  // generic canonical builder's extract leg never forwards include_justification
  // — the checkbox stopped rendering for this kind (classify keeps it).
  await expect(page.getByLabel('Include justification')).toHaveCount(0);

  const runRequest = page.waitForRequest(
    (request) =>
      request.url().includes(`/api/projects/${pid}/actions/v1/run`) && request.method() === 'POST',
  );
  await clickRunButton(page);
  const posted = (await runRequest).postDataJSON() as {
    params: { include_confidence?: boolean };
  };
  expect(posted.params.include_confidence).toBe(false);
});

test("a fresh classify launch also carries include_confidence/include_justification false unless toggled (item 6)", async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-classify-confidence-default'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'meetings.csv',
    [
      'title,notes',
      '"Budget hearing","Officials discussed vendor bids and transit funds."',
      '"Safety briefing","The police chief named two road closures."',
    ].join('\n'),
  );
  await mockV1ExtractRun(page, pid);
  await page.goto(`/p/${pid}/s/${sheetId}`);

  await openAction(page, 'map.classify');
  await expectAdditionalPromptAfterFields(page);
  await expect(page.getByTestId('action-prompt')).toHaveValue('');
  await page.getByTestId('action-prompt').fill('');
  await expect(page.getByTestId('run-button')).toBeEnabled();
  await expect(page.getByTestId('run-disabled-reason')).toHaveCount(0);
  await page.getByTestId('advanced-run-options-toggle').click();
  await expect(page.getByLabel('Include confidence')).not.toBeChecked();
  await expect(page.getByLabel('Include justification')).not.toBeChecked();

  const runRequest = page.waitForRequest(
    (request) =>
      request.url().includes(`/api/projects/${pid}/actions/v1/run`) && request.method() === 'POST',
  );
  await clickRunButton(page);
  const posted = (await runRequest).postDataJSON() as {
    params: {
      context?: string;
      include_confidence?: boolean;
      include_justification?: boolean;
    };
  };
  expect(posted.params.context).toBe('');
  expect(posted.params.include_confidence).toBe(false);
  expect(posted.params.include_justification).toBe(false);
});
