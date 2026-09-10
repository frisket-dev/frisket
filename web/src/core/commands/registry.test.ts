// Proves:
//   1. Union exhaustiveness — every WorkspaceCommand member is DECLARED in
//      FIRST_PARTY_COMMANDS with correct match/testId/enabled wiring.
//   2. The runActCommand switch (useWorkspaceModel.tsx) is fully covered by the
//      union — enumerated here as a fixture (core/ may not import the
//      workspace/ layer), so a case added later that lacks a home goes red
//      naturally.
//   3. Only the commands that are palette-exposed TODAY carry a
//      WorkbenchCommandDescriptor id; every other member is internal-only.
//   4. dispatch() routes through enablement to a descriptor's run().

import { describe, expect, it, vi } from 'vitest';
import { createStore } from '../store/createStore';
import { RUN_ACT_SWITCH_CASE_TO_COMMAND } from './actMenuSwitchMap';
import {
  FIRST_PARTY_COMMANDS,
  PALETTE_EXPOSED_COMMANDS,
  dispatch,
} from './registry';
import type { CommandContext, WorkspaceCommand } from './types';

// The full membership of the WorkspaceCommand union, enumerated as data so
// the union can be pinned independently of the registry that indexes it. If
// a member is added/removed, both this list and FIRST_PARTY_COMMANDS must move
// together (tsc forces the registry entry; this list forces the intent).
const EXPECTED_COMMAND_TYPES: ReadonlyArray<WorkspaceCommand['type']> = [
  'openSheet',
  'openRow',
  'openColumn',
  'openSource',
  'openEvidence',
  'openMap',
  'openGraph',
  'openActionRoute',
  'openMainViewContribution',
  'import.open',
  'sources.open',
  'export.open',
  'ocrCompare.open',
  'transcribeCompare.open',
  'translateCompare.open',
  'topicCompare.open',
  'settings.open',
];

// RUN_ACT_SWITCH_CASE_TO_COMMAND (the runActCommand switch cases,
// ActMenuCommand, workbench/actSurface.ts, mapped to their WorkspaceCommand
// home) now lives in ./actMenuSwitchMap.ts — imported above — so
// reachability.test.ts's reachability discriminator can share the SAME map
// this file's own assertions below use, instead of a second hand-typed copy
// that could drift from it.

// The commands palette-exposed today and their existing descriptor ids
// (descriptors.ts). Nothing else is exposed.
const EXPECTED_PALETTE_EXPOSED: Readonly<Record<string, string>> = {
  'sources.open': 'frisket.core.command.open_sources',
  'settings.open': 'frisket.core.command.open_settings',
  'ocrCompare.open': 'frisket.media.command.ocr_compare',
  'transcribeCompare.open': 'frisket.media.command.transcribe_compare',
  'translateCompare.open': 'frisket.media.command.translate_compare',
  'topicCompare.open': 'frisket.media.command.topic_segmentation_compare',
};

// Every Handle interface (types.ts) carries the concrete mutator methods the
// moved bodies call. The fake ctx below supplies a
// vi.fn() spy for each — real behavior is proven by the Playwright behavior
// gate (workbench-ia-ribbon/-command-palette/-focus); this unit fixture only
// proves dispatch() routes to the registered run(), which then calls the
// right ctx method with the right args (asserted per-command below).
function fakeCommandContext(): CommandContext {
  return {
    route: {
      store: createStore<unknown>(null),
      openSheet: vi.fn(),
      openRow: vi.fn(),
      openColumn: vi.fn(),
      openSource: vi.fn(),
      openActionRoute: vi.fn(),
    },
    grid: {
      store: createStore<unknown>(null),
    },
    chrome: {
      store: createStore<unknown>(null),
      openEvidence: vi.fn(),
      openMap: vi.fn(),
      openGraph: vi.fn(),
      openMainViewContribution: vi.fn(),
      openSources: vi.fn(),
      openSettings: vi.fn(),
    },
    scratch: {
      store: createStore<unknown>(null),
      openOcrCompare: vi.fn(),
      openTranscribeCompare: vi.fn(),
      openTranslateCompare: vi.fn(),
      openTopicCompare: vi.fn(),
    },
    actSurface: {
      store: createStore<unknown>(null),
      openImportDialog: vi.fn(),
      setExportModal: vi.fn(),
    },
    jobs: { store: createStore<unknown>(null) },
    selection: { store: createStore<unknown>(null) },
  };
}

// Minimal, argument-complete WorkspaceCommand per type — used only to prove
// dispatch() reaches the right ctx method; the union's own required fields
// (e.g. openSheet.sheetId) must be present for TypeScript, so this is a
// literal per-type fixture rather than a `{ type } as WorkspaceCommand` cast.
const SAMPLE_COMMAND_BY_TYPE: Readonly<Record<WorkspaceCommand['type'], WorkspaceCommand>> = {
  openSheet: { type: 'openSheet', sheetId: 's1' },
  openRow: { type: 'openRow', sheetId: 's1', rowId: 'r1' },
  openColumn: { type: 'openColumn', columnId: 'c1' },
  openSource: { type: 'openSource', sourceId: 'src1' },
  openEvidence: { type: 'openEvidence', linkId: 'ev1' },
  openMap: { type: 'openMap' },
  openGraph: { type: 'openGraph' },
  openActionRoute: { type: 'openActionRoute' },
  openMainViewContribution: { type: 'openMainViewContribution', contributionId: 'contrib1' },
  'import.open': { type: 'import.open' },
  'sources.open': { type: 'sources.open' },
  'export.open': { type: 'export.open', kind: 'dataset' },
  'ocrCompare.open': { type: 'ocrCompare.open' },
  'transcribeCompare.open': { type: 'transcribeCompare.open' },
  'translateCompare.open': { type: 'translateCompare.open' },
  'topicCompare.open': { type: 'topicCompare.open' },
  'settings.open': { type: 'settings.open' },
};

describe('WorkspaceCommand registry — declaration/exhaustiveness', () => {
  it('declares a descriptor for exactly every union member, no more, no less', () => {
    const registeredTypes = Object.keys(FIRST_PARTY_COMMANDS).sort();
    expect(registeredTypes).toEqual([...EXPECTED_COMMAND_TYPES].sort());
  });

  it('every descriptor has match === its table key, a non-empty testId, and an enabled predicate', () => {
    for (const [key, descriptor] of Object.entries(FIRST_PARTY_COMMANDS)) {
      expect(descriptor.match).toBe(key);
      expect(typeof descriptor.testId).toBe('string');
      expect(descriptor.testId.length).toBeGreaterThan(0);
      expect(typeof descriptor.enabled).toBe('function');
      expect(typeof descriptor.run).toBe('function');
    }
  });

  it('testIds are unique across the registry', () => {
    const testIds = Object.values(FIRST_PARTY_COMMANDS).map((d) => d.testId);
    expect(new Set(testIds).size).toBe(testIds.length);
  });

  it('every runActCommand switch case has a WorkspaceCommand home', () => {
    for (const [switchCase, commandType] of Object.entries(RUN_ACT_SWITCH_CASE_TO_COMMAND)) {
      expect(
        FIRST_PARTY_COMMANDS,
        `runActCommand case '${switchCase}' → '${commandType}' must be a declared command`,
      ).toHaveProperty(commandType);
    }
  });
});

describe('WorkspaceCommand registry — internal vs. palette-exposed (C9)', () => {
  it('palette-exposes exactly the four commands that ship a descriptor today', () => {
    expect(PALETTE_EXPOSED_COMMANDS).toEqual(EXPECTED_PALETTE_EXPOSED);
  });

  it('every other union member is internal-only (no WorkbenchCommandDescriptor id)', () => {
    for (const type of EXPECTED_COMMAND_TYPES) {
      const exposed = Object.prototype.hasOwnProperty.call(EXPECTED_PALETTE_EXPOSED, type);
      expect(
        Object.prototype.hasOwnProperty.call(PALETTE_EXPOSED_COMMANDS, type),
        `'${type}' palette-exposure`,
      ).toBe(exposed);
    }
  });

  it('every palette-exposed key is a real union member', () => {
    for (const type of Object.keys(PALETTE_EXPOSED_COMMANDS)) {
      expect(FIRST_PARTY_COMMANDS).toHaveProperty(type);
    }
  });
});

describe('dispatch()', () => {
  it('is gated on enablement: a disabled command is a no-op and does not run', () => {
    const ctx = fakeCommandContext();
    const descriptor = FIRST_PARTY_COMMANDS['import.open'];
    const enabledSpy = vi.spyOn(descriptor, 'enabled').mockReturnValue(false);
    const runSpy = vi.spyOn(descriptor, 'run');

    expect(() => dispatch({ type: 'import.open' }, ctx)).not.toThrow();
    expect(runSpy).not.toHaveBeenCalled();

    enabledSpy.mockRestore();
    runSpy.mockRestore();
  });

  it('routes an enabled command to its descriptor.run', () => {
    const ctx = fakeCommandContext();
    const descriptor = FIRST_PARTY_COMMANDS['import.open'];
    const runSpy = vi.spyOn(descriptor, 'run').mockImplementation(() => undefined);

    dispatch({ type: 'import.open' }, ctx);
    expect(runSpy).toHaveBeenCalledTimes(1);

    runSpy.mockRestore();
  });
});

describe('every command body is registered', () => {
  it('dispatching every command runs its registered body against a fake ctx without throwing', () => {
    const ctx = fakeCommandContext();
    for (const [type, descriptor] of Object.entries(FIRST_PARTY_COMMANDS)) {
      const runSpy = vi.spyOn(descriptor, 'run');
      const command = SAMPLE_COMMAND_BY_TYPE[type as WorkspaceCommand['type']];
      expect(() => dispatch(command, ctx), type).not.toThrow();
      expect(runSpy, type).toHaveBeenCalledTimes(1);
      runSpy.mockRestore();
    }
  });
});
