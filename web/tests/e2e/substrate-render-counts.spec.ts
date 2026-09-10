// Render-count harness for workspace context retirement. A `Profiler`-based
// gate was evaluated and REJECTED: React's
// `<Profiler onRender>` fires per COMMIT, not per function-component
// invocation, so isolating one named workbench region's render count from its
// siblings/children would require wrapping every region in its own
// `<Profiler>` boundary — strictly more invasive than the counter this spec
// reads, for no extra fidelity. Instead, `web/src/App.tsx` publishes
// `window.__renderCounts` (DEV-only, mirrors the `window.__frisketGridTheme`
// idiom at `web/src/grid/SheetGrid.tsx:461-472`): every top-level workbench
// region function calls `useRenderCount(<name>)` once per render, which
// increments a plain counter keyed by region name. Not a production API
// surface — gated on `import.meta.env.DEV`, same as the grid-theme hook.
//
// This spec drives ONE fixed interaction script: open sheet -> apply filter ->
// open drawer -> toggle theme. It reads
// the cumulative per-region counts after the script completes and enforces
// THREE gates: (1) PRESENT — every
// baselined region key must exist in the probe (a deleted useRenderCount must
// FAIL, not pass as an implicit 0); (2) CEILING — each count <= the checked-in
// baseline at ./render-counts.baseline.json; (3) MUST-DROP — each flipped,
// non-layout region is strictly below the pre-flip fan-out (16) AND is left at
// delta 0 by at least one specific unrelated store write.
//
// BASELINE PROVENANCE: originally captured on the PRE-flip code (all ten
// regions at 16 — WorkspaceViewContext fanning every region out through the
// 264-key return bag). Re-baselined TWICE, deliberately, each in the commit
// that changed the numbers (never silently in a later baseline diff):
//   1. after the shell split — chromeBar 16->2, act 16->8, navigate 16->0,
//      actionDrawer 16->4;
//   2. after layout memoization plus the mainView/overlay split with
//      memoized region bags (the FINAL post-migration table) —
//      inspectDetail/rightInspector/discover/bottomDock 16->8, overlay 16->8;
//      mainView stays at ceiling 16 (measured 14 warm: it hosts the grid, so
//      the script's filter and drawer steps are its REAL content — see the
//      must-drop exemption note below).
// Deterministic warm-run actuals sit 2 below most ceilings (act 6, the 8s
// measure 6, mainView 14); the ceilings hold the COLD-start worst case (the
// first script run after a fresh compile carries one extra availability
// settle render that only the catalog/layout-reading regions observe) so the
// <= gate never flakes across the ×2 serial requirement. Post-migration
// counts must only go DOWN: a region regressing above baseline means some
// flip widened its subscription surface instead of narrowing it —
// investigate before adjusting the baseline (never silently raise it).

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openFriendlyFilterSidebar,
  openCellDrawer,
  sheetColumns,
  uniqueName,
} from './helpers';

const baselinePath = fileURLToPath(new URL('./render-counts.baseline.json', import.meta.url));
const baseline: Record<string, number> = JSON.parse(readFileSync(baselinePath, 'utf8'));

test('render-counts: open sheet -> apply filter -> open drawer -> toggle theme stays <= baseline per region', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('substrate-render-counts'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status\nAlbany,open\nBuffalo,closed\nSyracuse,open\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);

  // --- open sheet ---
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });

  // The probe is installed by the FIRST render of each region, before this
  // spec's own interaction script begins. We ZERO EACH EXISTING KEY IN PLACE
  // (not `= {}`) so the counts below measure only this script's four steps,
  // NOT initial mount (mount cost is a SEPARATE, already-covered concern —
  // workbench-host-shell and region-frames-screenshots pin initial mount;
  // this spec pins re-render discipline under interaction, the thing
  // WorkspaceViewContext fan-out specifically breaks). Zeroing in place (vs.
  // dropping the object) is load-bearing for the PRESENT assertion below: a
  // region whose useRenderCount() was DELETED never creates its key at mount,
  // so it stays ABSENT and the PRESENT check fails — a deleted counter must
  // fail this spec, not silently pass as an implicit 0.
  const snapshot = () =>
    page.evaluate(
      () => ({ ...((window as unknown as { __renderCounts?: Record<string, number> }).__renderCounts ?? {}) }),
    );
  await page.evaluate(() => {
    const rc = (window as unknown as { __renderCounts?: Record<string, number> }).__renderCounts;
    if (rc) for (const key of Object.keys(rc)) rc[key] = 0;
  });

  // --- apply filter --- (a gridView store write; also resets selection)
  await openFriendlyFilterSidebar(page, columns, 'city');
  const filteredResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` && url.searchParams.has('filter');
  });
  await page.getByTestId('facet-check-city-Albany').check();
  await filteredResponse;
  await page.keyboard.press('Escape');
  const afterFilter = await snapshot();

  // --- open drawer --- (a detail/route/selection write)
  // 'city' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'city', 0);
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  const afterDrawer = await snapshot();

  // --- toggle theme --- (a chrome/theme write)
  await page.getByTestId('chrome-account').click();
  await page.getByTestId('account-appearance').click();
  await page.getByTestId('account-appearance-dark').click();
  await expect(page.locator('html')).toHaveAttribute('data-frisket-theme', 'dark');
  const afterTheme = await snapshot();

  const counts = afterTheme;
  const delta = (later: Record<string, number>, earlier: Record<string, number>, region: string) =>
    (later[region] ?? 0) - (earlier[region] ?? 0);
  const zeroed: Record<string, number> = {};
  for (const region of Object.keys(baseline)) zeroed[region] = 0;
  const filterDelta = (region: string) => delta(afterFilter, zeroed, region);
  const drawerDelta = (region: string) => delta(afterDrawer, afterFilter, region);
  const themeDelta = (region: string) => delta(afterTheme, afterDrawer, region);

  const regions = Object.keys(baseline);

  // (1) PRESENT: every baselined region's key must exist in the probe.
  // A deleted useRenderCount leaves the key absent (see zero-in-place above).
  const missing = regions.filter((region) => !(region in counts));
  expect(
    missing,
    `regions missing from __renderCounts (deleted useRenderCount? — a deleted counter must FAIL, not pass as 0):\n${missing.join('\n')}`,
  ).toEqual([]);

  // (2) CEILING: cumulative post-migration count <= the checked-in baseline.
  const overBaseline: string[] = [];
  for (const region of regions) {
    const actual = counts[region] ?? 0;
    const allowed = baseline[region];
    if (actual > allowed) {
      overBaseline.push(`${region}: ${actual} > baseline ${allowed}`);
    }
  }
  expect(overBaseline, `regions exceeding render-counts.baseline.json:\n${overBaseline.join('\n')}`).toEqual([]);

  // (3) MUST-DROP: every flipped region must be provably isolated:
  // (A) its whole-script total must be STRICTLY below the pre-flip fan-out
  // (16), not merely <= — pre-flip EVERY store write re-rendered EVERY region
  // through the WorkspaceViewContext/shell fan-out, pinning all ten at 16; a
  // region strictly below that proves the fan-out no longer reaches it; and
  // (B) at least one specific UNRELATED write leaves it at delta 0. The
  // unrelated write per region is what the region demonstrably does NOT
  // subscribe to, measured against the actual state touched by that step:
  //   - navigate    reads sheet tabs only -> BOTH the filter apply (gridView
  //                 write) AND the drawer open (detail/selection/route write)
  //                 must not re-render it (the strongest isolation in the suite)
  //   - chromeBar   reads grid filter/sort + copilot/export -> the drawer open
  //                 (a detail/selection/route store write) must not re-render
  //                 it. This is THE proof the 7b shell split landed: pre-split
  //                 the shared shell churned on drawer open and drove chromeBar
  //                 to 4 here; post-split it is 0.
  //   - every OTHER region (act's data-requirement catalog; actionDrawer's
  //                 run+selection; the four resolved-layout panels whose
  //                 availability shifts with selection; mainView's grid
  //                 surface; overlay's palette/host-context) legitimately
  //                 re-renders on filter and/or drawer — for them the
  //                 appearance toggle (a DOM/preferences write, NO workspace
  //                 store) is the write they must all ignore -> theme delta 0.
  //                 The resolved-layout panels are memoized and included here.
  // mainView is EXEMPT from the strict-below check only (its theme-delta-0
  // assertion stands): it hosts the grid, header menu, toolbar and banners, so
  // every script step except the theme toggle is its REAL content — a
  // per-member bag differ (7b flip-completion commit) confirmed each of its
  // residual renders corresponds to an actual read changing (header menu
  // open/close, applied filter, selection reset, layout availability), and a
  // cold first-compile settle can add +2, which would make a strict <16 check
  // flake without indicating any widened subscription.
  const PRE_FLIP_FANOUT = 16;
  const mustDrop: Array<{
    region: string;
    unrelated: Array<{ step: string; d: number }>;
    strictlyBelowFanout: boolean;
  }> = [
    { region: 'navigate', strictlyBelowFanout: true, unrelated: [
      { step: 'filter apply', d: filterDelta('navigate') },
      { step: 'drawer open', d: drawerDelta('navigate') },
    ] },
    { region: 'chromeBar', strictlyBelowFanout: true, unrelated: [
      { step: 'drawer open', d: drawerDelta('chromeBar') },
    ] },
    ...(['act', 'actionDrawer', 'inspectDetail', 'discover', 'rightInspector', 'bottomDock', 'overlay'] as const).map(
      (region) => ({
        region,
        strictlyBelowFanout: true,
        unrelated: [{ step: 'appearance toggle', d: themeDelta(region) }],
      }),
    ),
    { region: 'mainView', strictlyBelowFanout: false, unrelated: [
      { step: 'appearance toggle', d: themeDelta('mainView') },
    ] },
  ];
  const dropViolations: string[] = [];
  for (const { region, unrelated, strictlyBelowFanout } of mustDrop) {
    for (const { step, d } of unrelated) {
      if (d !== 0) {
        dropViolations.push(
          `${region}: re-rendered ${d}x on unrelated "${step}" (expected 0 — it does not read that state)`,
        );
      }
    }
    if (!strictlyBelowFanout) continue;
    const total = counts[region] ?? 0;
    if (!(total < PRE_FLIP_FANOUT)) {
      dropViolations.push(
        `${region}: total ${total} not strictly below pre-flip fan-out ${PRE_FLIP_FANOUT} (flip did not narrow its subscription surface)`,
      );
    }
  }
  expect(dropViolations, `must-drop violations:\n${dropViolations.join('\n')}`).toEqual([]);
});
