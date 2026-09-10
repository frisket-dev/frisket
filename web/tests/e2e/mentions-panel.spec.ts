// LIVE acceptance demonstrations for the Mentions panel.
//
// Everything here runs against the self-booted stack with the REAL spaCy
// engine (en_core_web_sm) — no stubbed runs, no injected entity JSON. Every
// entity, fingerprint, group and count below was produced by `map.ner` itself;
// the expected numbers were derived from the same pipeline before the spec was
// written and are asserted literally, so a change in grouping/counting fails
// here rather than being absorbed.
//
// Reading the GRID's row count: the grid itself is a canvas
// (glide-data-grid), so the filtered row count is asserted twice — once on the
// on-screen sheet heading (`sheet-stats`, "5 rows · 2 columns", which counts
// the VISIBLE rows) and once on the `total` of the grid's own filtered
// /sheets/{id}/data fetch, the request whose result the user is looking at.
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';

import {
  clickRunButton,
  createProject,
  importCsv,
  openAction,
  openDiscoverTab,
  openProject,
  openToolbarOverflow,
  runAndWait,
  sheetColumns,
  uniqueName,
} from './helpers';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();
const SHOT_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../test-results');

const MENTIONS_CONTRIBUTION = 'workbench-contribution-frisket-investigative-panel-mentions';

const shoot = (page: Page, name: string) =>
  page.screenshot({ path: path.join(SHOT_DIR, `mentions-panel-${name}.png`), animations: 'disabled' });

/** The Mentions panel as the user reaches it: the Discover panel's tab. */
async function openMentionsPanel(page: Page): Promise<Locator> {
  await openDiscoverTab(page, 'Mentions');
  const panel = page.getByTestId('discover-panel').getByTestId(MENTIONS_CONTRIBUTION);
  await expect(panel).toBeVisible();
  return panel;
}

/** Run `map.ner` server-side with the real spaCy engine. Used to SEED the
 *  demos that are not about the drawer (2–5); demo 1 drives the drawer. */
async function extractEntities(
  request: APIRequestContext,
  pid: string,
  sheetId: number,
  labels: string[],
): Promise<void> {
  await runAndWait(request, pid, {
    schema_version: 'frisket.action.v2',
    kind: 'map.ner',
    capabilities: ['project:write'],
    params: {
      sheet_id: sheetId,
      input_columns: ['snippet'],
      labels,
      engine: 'spacy',
      output_name: 'entities',
    },
  });
}

/** The row count the GRID lands on after `act()` applies a mention filter:
 *  the `total` of the grid's own filtered data fetch. */
async function gridRowsAfter(
  page: Page,
  pid: string,
  sheetId: number,
  act: () => Promise<void>,
): Promise<number> {
  const settled = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      (url.searchParams.get('filter') ?? '').includes('entity_eq') &&
      res.status() === 200
    );
  });
  await act();
  return ((await (await settled).json()) as { total: number }).total;
}

const csv = (rows: string[]) => `snippet\n${rows.map((row) => `"${row}"`).join('\n')}\n`;

// ---------------------------------------------------------------------------
// Demo 1 — fresh sheet → CTA → drawer → real extraction → grouped results

const DEMO1_ROWS = [
  'Maria Gomez sued Acme Corporation in Albany.',
  'ACME Corp. paid $4,000 to the city of Albany.',
  'Acme Corporation hired Maria Gomez in March 2024.',
  'The Buffalo office of ACME Corp. closed.',
];

test('1 — Mentions CTA opens the map.ner drawer, and a real spaCy run fills the panel', async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const pid = await createProject(request, uniqueName('e2e-mentions-cta'));
  const sheetId = await importCsv(request, pid, 'filings.csv', csv(DEMO1_ROWS));
  await openProject(page, pid, sheetId);

  const panel = await openMentionsPanel(page);
  await expect(panel.getByTestId('mentions-blurb')).toContainText(
    'This sheet has no entity column yet',
  );
  const cta = panel.getByTestId('mentions-extract-cta');
  await expect(cta).toHaveText('Extract entities');

  await cta.click();

  // The CTA OPENS the action drawer (and starts nothing on its own).
  const drawer = page.getByTestId('action-panel');
  await expect(drawer).toBeVisible({ timeout: 20_000 });
  expect((await sheetColumns(request, pid, sheetId)).map((column) => column.name)).toEqual([
    'snippet',
  ]);
  await shoot(page, '1-drawer');

  await expect(drawer).not.toContainText('Action unavailable');
  await expect(page.getByTestId('action-form')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('run-button')).toBeEnabled();

  // Engine spaCy, chosen explicitly through the picker.
  await page.getByTestId('engine-picker-button').click();
  await page.getByTestId('engine-option-spacy').click();
  await expect(page.getByTestId('engine-picker-button')).toContainText('spaCy');

  await clickRunButton(page, { requireCostConfirmation: false });
  await expectDemo1Results(panel, page);
});

/** The second half of demo 1 — drawer → real spaCy run → grouped results —
 *  reached through the Act ribbon instead of the panel CTA. Diagnostic, not a
 *  substitute: it isolates the failure above to the CTA's action kind by
 *  proving the same drawer, run and panel rendering work when the drawer is
 *  opened with the launcher kind the route expects. */
test('1b — the same map.ner drawer opened from the Act ribbon fills the Mentions panel', async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const pid = await createProject(request, uniqueName('e2e-mentions-ribbon'));
  const sheetId = await importCsv(request, pid, 'filings.csv', csv(DEMO1_ROWS));
  await openProject(page, pid, sheetId);
  const panel = await openMentionsPanel(page);
  await expect(panel.getByTestId('mentions-extract-cta')).toBeVisible();

  await openAction(page, 'map.ner');
  expect((await sheetColumns(request, pid, sheetId)).map((column) => column.name)).toEqual([
    'snippet',
  ]);
  await page.getByTestId('engine-picker-button').click();
  await page.getByTestId('engine-option-spacy').click();
  await expect(page.getByTestId('engine-picker-button')).toContainText('spaCy');
  await shoot(page, '1b-drawer');

  await clickRunButton(page, { requireCostConfirmation: false });

  // The run itself lands: the server's own read endpoint reports the finished
  // extraction, so anything still missing below is the PANEL, not map.ner.
  await expect.poll(
    async () => {
      const column = (await sheetColumns(request, pid, sheetId)).find(
        (candidate) => candidate.name === 'entities',
      );
      if (!column) return null;
      const res = await request.post(`/api/projects/${pid}/entity-mentions/v1/preview`, {
        data: { sheet_id: sheetId, column_id: column.id, limit: 10 },
      });
      if (!res.ok()) return null;
      const body = (await res.json()) as { total_groups: number; coverage: { completed_rows: number } };
      return { groups: body.total_groups, completed: body.coverage.completed_rows };
    },
    { timeout: 120_000, intervals: [500, 1_000, 2_000] },
  ).toEqual({ groups: 6, completed: 4 });
  await shoot(page, '1b-after-run');

  await expectDemo1Results(panel, page);
  await shoot(page, '1b-results');
});

async function expectDemo1Results(panel: Locator, page: Page): Promise<void> {
  // Real extraction: `Acme Corporation` (rows 1+3) and `ACME Corp.` (rows 2+4)
  // are one fingerprint group over all four rows; `ACME Corp.` is the label
  // (tied on distinct rows, shorter string).
  const acme = panel.getByTestId('mentions-group-organization-fingerprint-acme-corp');
  await expect(acme).toBeVisible({ timeout: 120_000 });
  await expect(acme).toContainText('ACME Corp.');
  await expect(acme).toContainText('2 forms');
  await expect(acme).toContainText('4 rows');
  await expect(acme).toContainText('4 mentions');

  // Canonical type headings, not raw OntoNotes tags.
  await expect(panel.getByTestId('mentions-type-organization')).toBeVisible();
  await expect(panel.getByTestId('mentions-type-person')).toContainText('Maria Gomez');
  await expect(panel.getByTestId('mentions-type-location')).toContainText('Albany');
  await expect(panel.getByTestId('mentions-coverage')).toHaveText('4 of 4 rows extracted');
  await shoot(page, '1-results');
}

// ---------------------------------------------------------------------------
// Demo 2 — disclose the grouped forms; grouped vs exact filter row counts
//
// Real spaCy output over these seven rows:
//   organization / fingerprint 'acme corp' — 5 rows, 6 mentions, 3 forms
//     ACME Corp.        3 rows / 3 mentions   (rows 1, 2, 7)
//     Acme Corporation  2 rows / 2 mentions   (rows 2, 3)
//     acme corp.        1 row  / 1 mention    (row 4)
//   organization / fingerprint 'acme inc'  — 2 rows, 2 mentions, 1 form
// `Acme Inc.` must NOT merge into `ACME Corp.`: the suffix alias table is
// closed, and Inc ≠ Corp.

const DEMO2_ROWS = [
  'ACME Corp. won a contract from the city of Albany.',
  'ACME Corp. and Acme Corporation appear in the same filing.',
  'Acme Corporation filed a permit in Buffalo.',
  'acme corp. paid a fine last year.',
  'Acme Inc. holds the lease on the warehouse.',
  'The county paid Acme Inc. for road salt.',
  'Maria Gomez signed the affidavit for ACME Corp.',
];
const DEMO2_LABELS = ['person', 'organization', 'location', 'date', 'money'];

async function seedDemo2(
  page: Page,
  request: APIRequestContext,
  name: string,
): Promise<{ pid: string; sheetId: number; panel: Locator }> {
  const pid = await createProject(request, uniqueName(name));
  const sheetId = await importCsv(request, pid, 'filings.csv', csv(DEMO2_ROWS));
  await extractEntities(request, pid, sheetId, DEMO2_LABELS);
  await openProject(page, pid, sheetId);
  const panel = await openMentionsPanel(page);
  return { pid, sheetId, panel };
}

test('2 — a multi-form group discloses its spellings; grouped and exact filters match their counts', async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const { pid, sheetId, panel } = await seedDemo2(page, request, 'e2e-mentions-forms');

  const grouped = panel.getByTestId('mentions-group-organization-fingerprint-acme-corp');
  await expect(grouped).toBeVisible({ timeout: 30_000 });
  await expect(grouped).toContainText('ACME Corp.');
  await expect(grouped).toContainText('3 forms');
  await expect(grouped).toContainText('5 rows');
  await expect(grouped).toContainText('6 mentions');

  // The deliberate non-merge, visible as its own group.
  const inc = panel.getByTestId('mentions-group-organization-fingerprint-acme-inc');
  await expect(inc).toContainText('Acme Inc.');
  await expect(inc).toContainText('2 rows');
  await expect(inc).not.toContainText('forms');

  // The caret is independent of the filter hit: expanding must not filter.
  await panel.getByTestId('mentions-group-expand-organization-fingerprint-acme-corp').click();
  const surfaces = panel.getByTestId('mentions-surfaces-organization-fingerprint-acme-corp');
  await expect(surfaces.getByTestId('mentions-surface-ACME-Corp')).toContainText('3 rows');
  await expect(surfaces.getByTestId('mentions-surface-Acme-Corporation')).toContainText('2 rows');
  await expect(surfaces.getByTestId('mentions-surface-acme-corp')).toContainText('1 row');
  await expect(surfaces.getByTestId('mentions-surface-ACME-Corp')).toContainText('exact spelling');
  await expect(page.getByTestId('active-grid-filter')).toHaveCount(0);
  await shoot(page, '2-expanded');

  // Grouped click → every spelling in the group.
  const groupedRows = await gridRowsAfter(page, pid, sheetId, () => grouped.click());
  expect(groupedRows).toBe(5);
  // The toolbar chip names the SPELLING the clicked group is known by: the
  // click carried it (the entity_eq payload holds only a fingerprint), so this
  // is no longer the same chip for every organization on the sheet.
  await expect(page.getByTestId('active-grid-filter')).toContainText(
    'entities · “Acme Corp.” (Organization, grouped forms)',
  );
  await expect(panel.getByTestId('mentions-active-filter')).toContainText(
    '“Acme Corp.” · Organization, grouped forms',
  );
  // Same number, on screen: the sheet heading counts the VISIBLE rows.
  await expect(page.getByTestId('sheet-stats')).toHaveText('5 rows · 2 columns');
  await shoot(page, '2-grouped-filter');

  // Exact-spelling click → only that literal surface, and it REPLACES the
  // grouped filter rather than ANDing with it (D8, single-select).
  const exactRows = await gridRowsAfter(page, pid, sheetId, () =>
    surfaces.getByTestId('mentions-surface-ACME-Corp').click(),
  );
  expect(exactRows).toBe(3);
  await expect(page.getByTestId('active-grid-filter')).toContainText(
    'entities · “ACME Corp.” (Organization, exact spelling)',
  );
  await expect(panel.getByTestId('mentions-active-filter')).toContainText(
    '“ACME Corp.” · Organization, exact spelling',
  );
  await expect(page.getByTestId('sheet-stats')).toHaveText('3 rows · 2 columns');
  await shoot(page, '2-exact-filter');
});

// ---------------------------------------------------------------------------
// Demo 3 — search a spelling that is NOT on the first result page
//
// 120 one-row organizations + one row carrying two spellings of a 121st.
// Every group has row_count 1, so the section orders by label; the target's
// label (`zephyr holdings corp.`, lower-case, shorter of its two forms) sorts
// last of 121 — page 1 shows 100 of them and never the target.

const DEMO3_ORGS = [
  'Aardvark', 'Alder', 'Amber', 'Anchor', 'Arbor', 'Ashford', 'Aspen', 'Atlas', 'Auburn', 'Avalon',
  'Bayside', 'Beacon', 'Birchwood', 'Bison', 'Blackstone', 'Bluefield', 'Bramble', 'Brentwood',
  'Briarwood', 'Bridgeport', 'Cardinal', 'Cascade', 'Cedar', 'Chestnut', 'Clearwater', 'Copper',
  'Cornerstone', 'Crestview', 'Cypress', 'Dalewood', 'Daybreak', 'Deerfield', 'Driftwood', 'Dunmore',
  'Eastgate', 'Edgewater', 'Elmwood', 'Emberly', 'Everett', 'Fairhaven', 'Falcon', 'Fernwood',
  'Flintlock', 'Foxglove', 'Foxtail', 'Gateway', 'Glenwood', 'Goldleaf', 'Granite', 'Greystone',
  'Halcyon', 'Harborview', 'Hawthorne', 'Hazelwood', 'Highpoint', 'Hollybrook', 'Ironwood',
  'Ivywood', 'Jasper', 'Juniper', 'Kestrel', 'Kingsley', 'Lakeshore', 'Larkspur', 'Laurelton',
  'Limestone', 'Longview', 'Maplewood', 'Marbury', 'Meridian', 'Millbrook', 'Northwind', 'Oakfield',
  'Oakhurst', 'Orchard', 'Pinecrest', 'Pinehurst', 'Quarrystone', 'Ravenwood', 'Redstone',
  'Ridgeline', 'Riverbend', 'Rockford', 'Rosewood', 'Sandpiper', 'Seabrook', 'Silverton',
  'Stonebridge', 'Summit', 'Sycamore', 'Tanglewood', 'Thistlewood', 'Thornbury', 'Timberline',
  'Trailhead', 'Umberton', 'Valeport', 'Vireo', 'Waterford', 'Westbrook', 'Wildwood', 'Willowbrook',
  'Windermere', 'Winterhaven', 'Woodhaven', 'Xanadu', 'Yarrow', 'Yorkfield', 'Zenithal', 'Ambleside',
  'Brookhaven', 'Castleton', 'Dunbarton', 'Eaglecrest', 'Fairmount', 'Glenbrook', 'Havenwood',
  'Inglewood', 'Kirkwood', 'Lyndhurst',
];

/** 121 organizations, of which the 121st (`zephyr holdings corp.`, two
 *  spellings) sorts last and so lands on page 2. Shared by demo 3 (server
 *  search reaches it) and demo 6 (paging to it survives a filter click). */
const demo3Rows = (): string[] => [
  ...DEMO3_ORGS.map((org) => `${org} Holdings Corporation filed a permit with the board.`),
  'Zephyr Holdings Corporation and zephyr holdings corp. both appear on the filing.',
];

test('3 — a spelling beyond the first page is found by server search, with whole-group counts', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const rows = demo3Rows();
  expect(rows).toHaveLength(121);

  const pid = await createProject(request, uniqueName('e2e-mentions-search'));
  const sheetId = await importCsv(request, pid, 'permits.csv', csv(rows));
  await extractEntities(request, pid, sheetId, ['organization']);
  await openProject(page, pid, sheetId);
  const panel = await openMentionsPanel(page);

  // The organization section opens on the top 25 of 121 — its heading states
  // the full count while the rest is one "Show more" away — and the target
  // (which sorts last) is not among them.
  const orgToggle = panel.getByTestId('mentions-type-toggle-organization');
  await expect(orgToggle).toContainText('121', { timeout: 30_000 });
  await expect(panel.getByTestId('mentions-group-row')).toHaveCount(25);
  await expect(panel.getByTestId('mentions-show-more-organization')).toHaveText('Show 96 more');
  const target = panel.getByTestId('mentions-group-organization-fingerprint-corp-holdings-zephyr');
  await expect(target).toHaveCount(0);
  await expect(panel).not.toContainText('Zephyr');
  await shoot(page, '3-page-one');

  const searched = page.waitForResponse((res) =>
    new URL(res.url()).pathname === `/api/projects/${pid}/entity-mentions/v1/preview` &&
    res.request().method() === 'POST' &&
    (res.request().postDataJSON() as { search?: string }).search === 'Zephyr Holdings Corporation',
  );
  await panel.getByTestId('mentions-search').fill('Zephyr Holdings Corporation');
  await searched;

  // The whole group comes back through the one matching spelling, with its
  // full-sheet counts (2 mentions), not a count of the matched spelling only.
  // The section now holds exactly it, and its heading count follows.
  await expect(orgToggle).toContainText('1');
  await expect(panel.getByTestId('mentions-group-row')).toHaveCount(1);
  await expect(panel.getByTestId('mentions-show-more-organization')).toHaveCount(0);
  await expect(target).toContainText('zephyr holdings corp.');
  await expect(target).toContainText('2 forms');
  await expect(target).toContainText('1 row');
  await expect(target).toContainText('2 mentions');

  await panel.getByTestId('mentions-group-expand-organization-fingerprint-corp-holdings-zephyr').click();
  const surfaces = panel.getByTestId('mentions-surfaces-organization-fingerprint-corp-holdings-zephyr');
  await expect(surfaces.getByTestId('mentions-surface-Zephyr-Holdings-Corporation')).toContainText(
    '1 mention',
  );
  await expect(surfaces.getByTestId('mentions-surface-zephyr-holdings-corp')).toContainText(
    '1 mention',
  );
  await shoot(page, '3-search');
});

// ---------------------------------------------------------------------------
// Demo 4 — a saved mention-filtered view survives leaving and coming back
// (the regression guard for the `entity_eq` silent-drop the review flagged).

test('4 — a saved view keeps its entity_eq mention filter through leave and restore', async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const { pid, sheetId, panel } = await seedDemo2(page, request, 'e2e-mentions-view');

  const grouped = panel.getByTestId('mentions-group-organization-fingerprint-acme-corp');
  await expect(grouped).toBeVisible({ timeout: 30_000 });
  expect(await gridRowsAfter(page, pid, sheetId, () => grouped.click())).toBe(5);
  await expect(page.getByTestId('active-grid-filter')).toContainText(
    'entities · “Acme Corp.” (Organization, grouped forms)',
  );

  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  await expect(page.getByTestId('views-panel')).toBeVisible();
  await page.getByTestId('create-saved-view').click();
  await page.getByTestId('view-name-input').fill('Acme mentions');
  const created = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views`) &&
    res.request().method() === 'POST' &&
    res.status() === 200,
  );
  await page.getByTestId('save-view-button').click();
  const saved = (await (await created).json()) as { spec: Record<string, unknown> };
  // The filter is PERSISTED structurally — not stringified, not dropped.
  expect(saved.spec).toEqual({
    filter: { entities: { entity_eq: { type: 'organization', fingerprint: 'acme corp' } } },
  });

  // Leave: back to the project root, filter cleared by the fresh mount.
  await page.goto('/');
  await expect(page.getByTestId('grid')).toHaveCount(0);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('active-grid-filter')).toHaveCount(0);

  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  const item = page.getByTestId('saved-view-item').filter({ hasText: 'Acme mentions' });
  await expect(item).toBeVisible();
  const restoredRows = await gridRowsAfter(page, pid, sheetId, () =>
    item.getByTestId('apply-saved-view').click(),
  );
  expect(restoredRows).toBe(5);

  // Still applied, and still described as a mention filter in both chips.
  // The TOOLBAR chip drops back to naming the type: a saved view persists the
  // filter SPEC, never the spelling that was clicked to make it, so carrying
  // one here would mean inventing it. The in-panel chip still names the
  // spelling because it resolves it from the groups it just loaded.
  await expect(page.getByTestId('active-grid-filter')).toContainText(
    'entities · Organization mentions (grouped forms)',
  );
  await expect(page.getByTestId('sheet-stats')).toHaveText('5 rows · 2 columns');
  const restoredPanel = await openMentionsPanel(page);
  await expect(restoredPanel.getByTestId('mentions-active-filter')).toContainText(
    '“Acme Corp.” · Organization, grouped forms',
  );
  await shoot(page, '4-restored-view');
});

// ---------------------------------------------------------------------------
// Demo 5 — a legacy (unmarked) entity column offers Rebuild, never fallback
//
// A pre-marker `map.ner` column cannot be produced through the API any more:
// every `map.ner` run stamps `semantic_type='entity_mentions'` on its output.
// So the legacy state is reproduced the only way it exists in the wild — a
// real `map.ner` column whose marker is absent — by clearing the marker
// directly in the project store (the same out-of-band store access
// column-format.spec.ts and friends already use).

function clearSemanticTypeMarker(pid: string, columnId: string): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import sys
from pathlib import Path
from frisket.engine.store import Project

workspace, pid, column_id = sys.argv[1], sys.argv[2], int(sys.argv[3])
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    project.set_column_semantic_type(column_id, None)
    row = project.db.execute(
        "SELECT semantic_type, current_run_id FROM columns WHERE id=?", (column_id,)
    ).fetchone()
    print(row["semantic_type"], row["current_run_id"])
finally:
    project.close()
`;
  execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, columnId], {
    cwd: REPO_ROOT,
    stdio: 'pipe',
    timeout: 60_000,
  });
}

test('5 — a legacy entity column offers Rebuild and shows none of its old items', async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const pid = await createProject(request, uniqueName('e2e-mentions-legacy'));
  const sheetId = await importCsv(request, pid, 'filings.csv', csv(DEMO2_ROWS));
  await extractEntities(request, pid, sheetId, DEMO2_LABELS);

  const entities = (await sheetColumns(request, pid, sheetId)).find(
    (column) => column.name === 'entities',
  );
  expect(entities).toBeTruthy();
  clearSemanticTypeMarker(pid, String(entities!.id));

  await openProject(page, pid, sheetId);
  const panel = await openMentionsPanel(page);

  const rebuild = panel.getByTestId('mentions-rebuild-cta');
  await expect(rebuild).toHaveText('Rebuild entity column');
  await expect(panel.getByTestId('mentions-blurb')).toContainText(
    'extracted before mention grouping',
  );

  // No fallback: the old items are not parsed, fingerprinted, or listed.
  await expect(panel.getByTestId('mentions-extract-cta')).toHaveCount(0);
  await expect(panel.getByTestId('mentions-group-row')).toHaveCount(0);
  await expect(panel.getByTestId('mentions-summary')).toHaveCount(0);
  await expect(panel).not.toContainText('ACME Corp.');
  await expect(panel).not.toContainText('Acme Inc.');
  await expect(panel).not.toContainText('Maria Gomez');
  await shoot(page, '5-rebuild');

  // Rebuilding is a fresh whole-column extraction, opened for review.
  await rebuild.click();
  await expect(page.getByTestId('action-panel')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('action-panel')).not.toContainText('Action unavailable');
  await expect(page.getByTestId('action-form')).toBeVisible({ timeout: 20_000 });
});

// ---------------------------------------------------------------------------
// Demo 6 — the pages a user loaded survive a filter click
//
// Applying a mention filter is a VIEW-scope change, so the host's
// `dataVersion` must NOT move and the panel must keep every page in hand.
// The regression this guards is specific and was found by review rather than
// by a test: a filter click reset paging to page 1, which HID the very group
// that was just clicked — the panel dropped the user's own selection off
// screen at the moment it took effect.

test('6 — filtering by a group that only exists on page 2 keeps both pages loaded', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const pid = await createProject(request, uniqueName('e2e-mentions-paging'));
  const sheetId = await importCsv(request, pid, 'permits.csv', csv(demo3Rows()));
  await extractEntities(request, pid, sheetId, ['organization']);
  await openProject(page, pid, sheetId);
  const panel = await openMentionsPanel(page);

  // Page 1: the organization section opens on 25 of 121, its heading stating
  // the full count, and the target group is genuinely not among them.
  const target = panel.getByTestId('mentions-group-organization-fingerprint-corp-holdings-zephyr');
  const orgToggle = panel.getByTestId('mentions-type-toggle-organization');
  await expect(orgToggle).toContainText('121', { timeout: 30_000 });
  await expect(panel.getByTestId('mentions-group-row')).toHaveCount(25);
  await expect(target).toHaveCount(0);

  // The rest of the section, loaded the way the user loads it: this category's
  // own "Show more", inside the section rather than a global button.
  await expect(panel.getByTestId('mentions-show-more-organization')).toHaveText('Show 96 more');
  await panel.getByTestId('mentions-show-more-organization').click();
  await expect(panel.getByTestId('mentions-group-row')).toHaveCount(121);
  await expect(target).toBeVisible();
  await expect(panel.getByTestId('mentions-show-more-organization')).toHaveCount(0);

  // The filter click itself lands on the grid.
  expect(await gridRowsAfter(page, pid, sheetId, () => target.click())).toBe(1);

  // …and the panel is unchanged around it: the whole section still in hand, the
  // clicked group still on screen, still marked as the active selection, and no
  // "Show more" reappearing to say the loaded page was thrown away.
  await expect(panel.getByTestId('mentions-group-row')).toHaveCount(121);
  await expect(target).toBeVisible();
  await expect(target).toHaveAttribute('aria-pressed', 'true');
  await expect(panel.getByTestId('mentions-show-more-organization')).toHaveCount(0);
  await expect(panel.getByTestId('mentions-active-filter')).toContainText(
    '“Acme Corp.” · Organization, grouped forms',
  );
  await shoot(page, '6-paging-survives-filter');

  // The same single row, read off the sheet heading the way demos 2 and 4 read
  // theirs — the one place this suite has only ever landed on a PLURAL count.
  await expect(page.getByTestId('sheet-stats')).toHaveText('1 row · 2 columns');
});

// ---------------------------------------------------------------------------
// Demo 7 — a type section is HEADED by the words the NER form offered
//
// The test ids stay keyed on the RAW canonical type (`norp`, `work_of_art`) —
// they identify the section, they do not present it — so a heading that
// silently reverted to the raw type would still be found by id and must be
// caught on its visible text.

const DEMO7_ROWS = [
  'A Republican senator met an American delegation in Albany.',
  'Irish and Canadian groups joined the Democratic rally.',
  'She read a copy of Hamlet on the train.',
  'The council approved a copy of Hamlet for the school library.',
];

test('7 — type headings read as their names, not as norp / work_of_art', async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const pid = await createProject(request, uniqueName('e2e-mentions-headings'));
  const sheetId = await importCsv(request, pid, 'filings.csv', csv(DEMO7_ROWS));
  await extractEntities(request, pid, sheetId, ['norp', 'work_of_art']);
  await openProject(page, pid, sheetId);
  const panel = await openMentionsPanel(page);

  const norp = panel.getByTestId('mentions-type-norp');
  await expect(norp).toBeVisible({ timeout: 30_000 });
  await expect(panel.getByTestId('mentions-type-toggle-norp')).toContainText(
    'Nationalities, religious & political groups',
  );
  await expect(norp).toContainText('Republican');
  await expect(norp).toContainText('Canadian');

  const art = panel.getByTestId('mentions-type-work_of_art');
  await expect(panel.getByTestId('mentions-type-toggle-work_of_art')).toContainText('Works of art');
  await expect(art).toContainText('Hamlet');

  // The raw type is nowhere on screen — not as the heading, not beside it.
  await expect(panel).not.toContainText('norp');
  await expect(panel).not.toContainText('NORP');
  await expect(panel).not.toContainText('work_of_art');
  await expect(panel).not.toContainText('WORK_OF_ART');
  await shoot(page, '7-readable-headings');
});
