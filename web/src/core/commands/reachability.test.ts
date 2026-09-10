// Glob self-registration (registry.ts, import.meta.glob('./first-party/
// *.command.ts', { eager: true })) makes every first-party command's
// `export default descriptor` genuinely load-bearing at runtime but
// invisible to deslop's static unused-export analysis — deslop cannot see a
// module reached only through import.meta.glob, so it flags them all as
// unused. Most are false positives (the glob IS the real caller); a blind
// per-directory doctor.config.ts override would hide that risk though: a
// command that's glob-*registered* but never actually *dispatched* from any
// real UI trigger is genuine dead code, not a false positive, and a
// directory-wide suppression can't tell the two apart.
//
// This test is the per-command check that can. A registered command is live
// iff it is reachable via >= 1 of:
//   1. the command palette's exposure table — PALETTE_EXPOSED_COMMANDS
//      (registry.ts);
//   2. the Act-menu switch map — RUN_ACT_SWITCH_CASE_TO_COMMAND
//      (actMenuSwitchMap.ts — the SAME module registry.test.ts's own
//      exhaustiveness assertions import, not a second hand-typed copy that
//      could drift from it). Appearing in the switch map only counts if the
//      switch CASE is itself reachable — every map key must be an
//      ActMenuCommand value the actSurface resolver can actually emit
//      (EMITTABLE_ACT_MENU_COMMANDS below). A weaker check that only asked
//      "does a dispatch site exist" was evaded by nine commands whose only
//      dispatch site was an unreachable
//      switch case (view.grid/map/graph, discover.toggle, wrap.toggle,
//      rowHeight.cycle, savedViews.toggle, provenance.toggle, palette.open —
//      all deleted, see REMOVED_COMMAND_TYPES);
//   3. hostContext.navigation call sites — the concrete dispatchCommand(...)
//      wrappers built in useWorkspaceModel.tsx's workbenchHostContext.navigation
//      (today: openSheet/openRow/openColumn/openSource/openEvidence/openMap/
//      openGraph/openActionRoute/openMainViewContribution) — core/ tests may
//      not import workspace/, so this is a grep-verified literal list, same
//      convention as #2's mirrored fixture;
//   4. an explicit, individually-justified internal-only allowlist for a
//      command legitimately registered-but-not-UI-exposed. Empty today: every
//      currently-registered command reaches path 1, 2, or 3. Add an entry
//      here ONLY with a real one-line reason (e.g. "dispatched only from
//      inside another command's run() as an internal sub-step") — a blanket
//      "internal" label defeats the discriminator's whole point.
//
// Any command that fails all four is real dead code: delete it or wire it,
// never silently keep it via a config override.
//
// Once every glob-registered command passes this test, doctor.config.ts's
// `core/commands/first-party/**` unused-export override (and the sibling
// `core/selectors/views/**` override for the work-view glob family) is an
// HONEST disposition — this test is the guard that keeps it honest: it goes
// red the moment a newly-added command file lands without being wired
// anywhere real.

import { describe, expect, it } from 'vitest';
import { RUN_ACT_SWITCH_CASE_TO_COMMAND } from './actMenuSwitchMap';
import { FIRST_PARTY_COMMANDS, PALETTE_EXPOSED_COMMANDS } from './registry';
import type { WorkspaceCommand } from './types';

// Path 2's reachability precondition — the ActMenuCommand values the shared
// Act model can actually emit: exactly the `command` entries RIBBON_LAYOUT's
// fixed groups declare (workbench/actSurface.ts; the ActMenuCommand union is
// pinned to this same set). core/ tests may not import workbench/, so this is
// a grep-verified mirror, same convention as the navigation list below. A
// RUN_ACT_SWITCH_CASE_TO_COMMAND key outside this set would be an unreachable
// switch case, and the pin below goes red on it.
const EMITTABLE_ACT_MENU_COMMANDS: readonly string[] = [
  'import',
  'sources',
  'export-csv',
  'export-google-sheets',
  'export-column-tables',
  'ocr-compare',
  'transcribe-compare',
  'translate-compare',
  'topic-compare',
];

// Path 3 — hostContext.navigation. Verified against the real call sites:
// useWorkspaceModel.tsx's workbenchHostContext.navigation section is the ONLY
// place any of these types are ever passed to dispatchCommand() outside of
// runActCommand (path 2) and the palette (path 1); grepped clean
// (`grep -rn "type: '<type>'" web/src`) for each entry below.
const NAVIGATION_DISPATCHED_COMMANDS: ReadonlySet<WorkspaceCommand['type']> = new Set([
  'openSheet',
  'openRow',
  'openColumn',
  'openSource',
  'openEvidence',
  'openMap',
  'openGraph',
  'openActionRoute',
  'openMainViewContribution',
]);

// Path 4 — explicit internal-only allowlist. Empty today.
const INTERNAL_ONLY_ALLOWLIST: Readonly<Partial<Record<WorkspaceCommand['type'], string>>> = {};

// Tombstones: commands deleted because their only dispatch site was an
// unreachable runActCommand switch case, plus the ActMenuCommand values no
// renderer could emit. Pinned ABSENT so the genre cannot silently return:
// re-adding any of these requires wiring a real dispatch surface first
// (which makes the pins below legitimately editable).
const REMOVED_COMMAND_TYPES: readonly string[] = [
  'view.grid',
  'view.map',
  'view.graph',
  'discover.toggle',
  'wrap.toggle',
  'rowHeight.cycle',
  'savedViews.toggle',
  'provenance.toggle',
  'palette.open',
  'workView.set',
];
const REMOVED_ACT_MENU_COMMANDS: readonly string[] = [
  'export-root',
  'view-grid',
  'view-map',
  'view-graph',
  'toggle-discover',
  'toggle-wrap',
  'row-height',
  'saved-views',
  'provenance',
  'help-docs',
  'help-shortcuts',
];

function isReachable(type: WorkspaceCommand['type']): boolean {
  if (Object.prototype.hasOwnProperty.call(PALETTE_EXPOSED_COMMANDS, type)) return true;
  if (Object.values(RUN_ACT_SWITCH_CASE_TO_COMMAND).includes(type)) return true;
  if (NAVIGATION_DISPATCHED_COMMANDS.has(type)) return true;
  if (Object.prototype.hasOwnProperty.call(INTERNAL_ONLY_ALLOWLIST, type)) return true;
  return false;
}

describe('glob-registered command reachability (F7 discriminator)', () => {
  it('every FIRST_PARTY_COMMANDS entry is reachable via >= 1 of: palette exposure, Act-menu map, hostContext.navigation, or the explicit internal-only allowlist', () => {
    const registeredTypes = Object.keys(FIRST_PARTY_COMMANDS) as WorkspaceCommand['type'][];
    const unreachable = registeredTypes.filter((type) => !isReachable(type));

    expect(
      unreachable,
      'these commands are glob-registered but fail every reachability path — ' +
        'real dead code (delete or wire it), not a glob false positive',
    ).toEqual([]);
  });

  it('path 2 is itself reachable: every Act-menu switch-map key is an ActMenuCommand value the actSurface resolver can emit (no unreachable-switch dispatch sites)', () => {
    // The strengthened check: a dispatch site only counts as a reachability
    // path if the site can actually run. The switch
    // map's key set must EQUAL the emittable mirror — a key outside it is an
    // unreachable case; a missing key is an emittable command with no home.
    expect([...Object.keys(RUN_ACT_SWITCH_CASE_TO_COMMAND)].sort()).toEqual(
      [...EMITTABLE_ACT_MENU_COMMANDS].sort(),
    );
  });

  it('the Lane-F1 removals stay gone: no removed command id reappears in the registry or any reachability table', () => {
    const registered = new Set(Object.keys(FIRST_PARTY_COMMANDS));
    const switchTargets = new Set(Object.values(RUN_ACT_SWITCH_CASE_TO_COMMAND));
    for (const type of REMOVED_COMMAND_TYPES) {
      expect(registered.has(type), `'${type}' must stay unregistered`).toBe(false);
      expect(
        Object.prototype.hasOwnProperty.call(PALETTE_EXPOSED_COMMANDS, type),
        `'${type}' must stay palette-unexposed`,
      ).toBe(false);
      expect(switchTargets.has(type as WorkspaceCommand['type']),
        `'${type}' must stay out of the Act-menu switch map`,
      ).toBe(false);
      expect(
        NAVIGATION_DISPATCHED_COMMANDS.has(type as WorkspaceCommand['type']),
        `'${type}' must stay out of the navigation list`,
      ).toBe(false);
    }
    for (const actCommand of REMOVED_ACT_MENU_COMMANDS) {
      expect(
        Object.prototype.hasOwnProperty.call(RUN_ACT_SWITCH_CASE_TO_COMMAND, actCommand),
        `Act-menu case '${actCommand}' must stay removed`,
      ).toBe(false);
      expect(
        EMITTABLE_ACT_MENU_COMMANDS.includes(actCommand),
        `'${actCommand}' must stay out of the emittable mirror`,
      ).toBe(false);
    }
  });

  it('the four reachability paths do not silently drift empty (each path table is non-trivial or explicitly justified)', () => {
    // Path 4 is legitimately empty today — assert its shape rather than its
    // size so this test does not need updating the moment a real entry lands.
    expect(typeof INTERNAL_ONLY_ALLOWLIST).toBe('object');
    // Paths 1-3 gate real UI surfaces and should never silently go empty.
    expect(Object.keys(PALETTE_EXPOSED_COMMANDS).length).toBeGreaterThan(0);
    expect(Object.keys(RUN_ACT_SWITCH_CASE_TO_COMMAND).length).toBeGreaterThan(0);
    expect(NAVIGATION_DISPATCHED_COMMANDS.size).toBeGreaterThan(0);
  });
});
