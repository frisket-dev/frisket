// derive.join UI: the ribbon "Join sheets (by key)" launcher, its
// isTabularJoin ActionPanel layout (two full sheet pickers, a repeatable
// key-pair row list, a how= select, an indicator toggle), and the fan-out
// confirm surfaced through the shared costGate UI. The backend (derive-join-
// action-v1) is real, deterministic, and non-model (cost: none) — unlike
// semantic-join.spec.ts, these specs drive the REAL backend end to end with no
// route mocking.

import { expect, test, type APIRequestContext } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  openAction,
  openProject,
  postAction,
  sheetData,
  typedActionRequest,
  uniqueName,
} from './helpers';

async function importNamedCsv(
  request: APIRequestContext,
  pid: string,
  sheetName: string,
  csv: string,
): Promise<number> {
  const res = await request.post(`/api/projects/${pid}/import/csv?sheet_name=${sheetName}`, {
    multipart: {
      file: { name: `${sheetName}.csv`, mimeType: 'text/csv', buffer: Buffer.from(csv) },
    },
  });
  expect(res.ok()).toBeTruthy();
  return (await res.json()).sheet_id;
}


// A small pandas-parity fixture: one shared key (NY, CA) on both
// sides, one left-only row (ZZ), one right-only row (YY). how= row counts:
// inner=2, left=3, right=3, outer=4 — small enough to hand-verify, distinct
// enough per how= that a wrong join semantics fails loudly.
async function seedStatesAndPopulation(
  request: APIRequestContext,
  pid: string,
): Promise<{ leftId: number; rightId: number }> {
  const leftId = await importNamedCsv(
    request,
    pid,
    'States',
    'code,name\nNY,New York\nCA,California\nZZ,ZeroMatch\n',
  );
  const rightId = await importNamedCsv(
    request,
    pid,
    'Population',
    'code,pop\nNY,100\nCA,200\nYY,999\n',
  );
  return { leftId, rightId };
}

test('derive.join form posts multi-pair join_keys and creates a joined sheet', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('derive-join-multipair'));
  // Two key columns (code, kind): naive single-key (code) matching would fan
  // out NY to 2*2=4 rows; the correct AND-of-both-pairs match yields exactly
  // 3 — proof the SECOND pair actually posted and was applied, not just that
  // the request shape looked right.
  const leftId = await importNamedCsv(
    request,
    pid,
    'Cities',
    'code,kind,city,ignored_left\nNY,state,Albany,a\nNY,county,Buffalo,b\nCA,state,Sacramento,c\n',
  );
  const rightId = await importNamedCsv(
    request,
    pid,
    'Zones',
    'code,kind,zone,ignored_right\nNY,state,East,x\nNY,county,West,y\nCA,state,Pacific,z\n',
  );

  const posted: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    if (body.action_id === 'derive.join') posted.push(body);
    await route.continue();
  });

  await openProject(page, pid, leftId);
  await openAction(page, 'derive.join');
  await expect(page.getByTestId('generated-action-form')).toBeVisible();
  await expect(page.getByTestId('tabular-join-form')).toBeVisible();

  // The left side is the sheet the action was launched from; the right side
  // defaults to the only other sheet.
  await expect(page.getByTestId('tabular-join-form')).toContainText('Left: Cities.');
  await expect(page.getByTestId('field-join_right_sheet')).toHaveValue(String(rightId));

  // Pair 0 is open by default and uses the first column on each side (code/code).
  await expect(page.getByTestId('join-key-pair-0')).toBeVisible();
  await expect(page.getByTestId('field-join_key_left-0')).toHaveValue('code');
  await expect(page.getByTestId('field-join_key_right-0')).toHaveValue('code');

  // Pair 1 also auto-defaults to code/code (joinLeftColumns[0]/joinRightColumns[0])
  // — explicitly repoint it to kind/kind, the second half of the composite key.
  await page.getByTestId('join-key-pair-add').click();
  await expect(page.getByTestId('join-key-pair-1')).toBeVisible();
  await page.getByTestId('field-join_key_left-1').selectOption('kind');
  await page.getByTestId('field-join_key_right-1').selectOption('kind');

  // Select an explicit output projection from both sides. The ignored_*
  // columns make the real result schema prove these controls affected the
  // backend rather than merely appearing in the posted request.
  await page.getByTestId('field-join_left_columns').click();
  await page
    .getByTestId('field-join_left_columns-menu')
    .getByRole('option')
    .filter({ hasText: /^city/ })
    .click();
  await page.getByTestId('field-how').click();
  await expect(page.getByTestId('field-join_left_columns')).toContainText('city');

  await page.getByTestId('field-join_right_columns').click();
  await page
    .getByTestId('field-join_right_columns-menu')
    .getByRole('option')
    .filter({ hasText: /^zone/ })
    .click();
  await page.getByTestId('field-how').click();
  await expect(page.getByTestId('field-join_right_columns')).toContainText('zone');

  await page.getByTestId('field-sheet_name').fill('Cities x Zones');

  const response = page.waitForResponse((r) =>
    r.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
    r.request().method() === 'POST',
  );
  await clickRunButton(page);
  const out = await response;
  const bodyText = await out.text();
  expect(out.ok(), bodyText).toBeTruthy();
  const result = JSON.parse(bodyText);
  expect(result.schema_version).toBe('frisket.action_result.v1');
  expect(result.action.kind).toBe('derive.join');
  expect(result.status, bodyText).toBe('completed');
  expect(result.receipt_id).toBeTruthy();

  expect(posted).toHaveLength(1);
  const [postedAction] = posted;
  expect(postedAction.scope).toEqual({ kind: 'sheet_rows', sheet_id: leftId });
  expect(postedAction.sheet_name).toBe('Cities x Zones');
  expect(postedAction).not.toHaveProperty('confirmation');
  const params = postedAction.params as Record<string, unknown>;
  expect(params.right).toEqual({ sheet_id: rightId });
  expect(params.join_keys).toEqual([
    { left_column: 'code', right_column: 'code' },
    { left_column: 'kind', right_column: 'kind' },
  ]);
  expect(params.columns).toEqual([
    { side: 'left', column: 'city' },
    { side: 'right', column: 'zone' },
  ]);
  expect(params.how).toBe('inner');
  expect(params.indicator).toBe(false);

  const output = (result.outputs as Array<Record<string, unknown>>).find((o) => o.kind === 'sheet');
  expect(output).toBeTruthy();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${output!.sheet_id}(?:[/?#]|$)`));
  const joined = await sheetData(request, pid, Number(output!.sheet_id));
  expect(joined.total).toBe(3); // NOT 4 — proves the composite (code, kind) key, not a code-only fan-out.
  const columnNames = joined.columns.map((c) => c.name).sort();
  expect(columnNames).toEqual(['city', 'code', 'kind', 'zone']);
});

test('the lineage panel shows edges to both parent sheets', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('derive-join-lineage'));
  const { leftId, rightId } = await seedStatesAndPopulation(request, pid);

  const result = await postAction(request, pid, typedActionRequest('derive.join', {
    sheetId: leftId,
    sheetName: 'States x Pop (lineage)',
    params: {
      right: { sheet_id: rightId },
      join_keys: [{ left_column: 'code', right_column: 'code' }],
      how: 'inner',
    },
  }));
  expect(result.status).toBe('completed');
  const output = result.outputs?.find((o) => o.kind === 'sheet');
  const childId = Number(output?.sheet_id);

  await openProject(page, pid, childId);
  await page.getByTestId('bottom-dock-tab-lineage').click();
  await expect(page.getByTestId('lineage-panel')).toBeVisible();

  await expect(page.getByTestId(`lineage-node-sheet-${childId}`)).toBeVisible();
  await expect(page.getByTestId(`lineage-node-sheet-${leftId}`)).toBeVisible();
  await expect(page.getByTestId(`lineage-node-sheet-${rightId}`)).toBeVisible();
  await expect(page.getByTestId(`lineage-edge-sheet-${leftId}-sheet-${childId}`)).toBeVisible();
  await expect(page.getByTestId(`lineage-edge-sheet-${rightId}-sheet-${childId}`)).toBeVisible();
});

test('the fan-out guard surfaces the shared costGate confirm UI and re-submits with its consent token', async ({ page, request }) => {
  // derive.join's row-count fan-out guard rides the SAME unified
  // HTTP 402 needs_confirmation envelope as the model-cost gate:
  // action_runtime.py's
  // deterministic owned-write body maps the marked resolve_fn ActionError onto
  // status="needs_confirmation", api/real.ts parses it into
  // ConfirmationRequiredError (estimated_rows -> estimate.rows, cost stays
  // null), and useRunController routes it through the one costGate/CostGateModal
  // primitive — no bespoke ApiError-code branch. 1001 duplicate-keyed rows on
  // each side inner-fan to 1001*1001 = 1,002,001 rows, over the 1,000,000
  // default max_output_rows.
  const pid = await createProject(request, uniqueName('derive-join-fanout'));
  const dupCsv = (label: string): string => {
    const lines = ['code,label'];
    for (let i = 0; i < 1001; i += 1) lines.push(`DUP,${label}${i}`);
    return `${lines.join('\n')}\n`;
  };
  const leftId = await importNamedCsv(request, pid, 'BigLeft', dupCsv('L'));
  const rightId = await importNamedCsv(request, pid, 'BigRight', dupCsv('R'));

  await openProject(page, pid, leftId);
  await openAction(page, 'derive.join');
  await expect(page.getByTestId('tabular-join-form')).toBeVisible();
  await expect(page.getByTestId('field-join_right_sheet')).toHaveValue(String(rightId));
  await page.getByTestId('field-sheet_name').fill('Big Fanout');

  // The challenge's promise-set hash, and the confirming re-post captured from
  // the REQUEST as it is sent (not the response), since actually writing ~1M
  // rows in one transaction takes well past any reasonable e2e wait; the
  // request-level proof is what this test is for.
  let challengeHash: string | null = null;
  page.on('response', async (res) => {
    if (!res.url().includes(`/api/projects/${pid}/actions/v1/run`) || res.status() !== 402) return;
    const body = (await res.json()) as { errors?: Array<{ details?: { promise_set_hash?: string } }> };
    challengeHash = body.errors?.[0]?.details?.promise_set_hash ?? null;
  });
  let confirmedBody: Record<string, unknown> | null = null;
  page.on('request', (req) => {
    if (!req.url().includes(`/api/projects/${pid}/actions/v1/run`) || req.method() !== 'POST') return;
    const body = req.postDataJSON() as Record<string, unknown>;
    if (body.action_id === 'derive.join' && typeof body.confirmation === 'string') confirmedBody = body;
  });

  await page.getByTestId('generated-action-run').click();
  const modal = page.getByTestId('cost-gate-modal');
  await expect(modal).toBeVisible({ timeout: 15_000 });
  // No dollar cost (derive.join is cost:none) — the modal renders the row
  // estimate instead, reusing the SAME "Estimated cost: … · N rows" line.
  await expect(page.getByTestId('cost-gate-estimate')).toContainText('1,002,001 rows');
  await page.getByTestId('cost-gate-confirm').click();

  await expect.poll(() => confirmedBody !== null, { timeout: 10_000 }).toBeTruthy();
  expect(challengeHash).toBeTruthy();
  expect(confirmedBody!.confirmation).toBe(challengeHash);
  expect(confirmedBody!.sheet_name).toBe('Big Fanout');
});
