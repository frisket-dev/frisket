import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// Covers the pure cross-store transition component.
//
// Contract: one pure cross-store transition closes Copilot
// and opens import directly. It must not call the normal Copilot popover
// dismiss path, which schedules focus restoration to the Copilot trigger and
// races the import dialog. Import owns focus. So a single pure function reads
// the chrome + actSurface slices and returns the next slices plus an explicit
// `refocusCopilotTrigger:false` marker — it closes `copilotPopoverOpen`, opens
// the single global `importDialogOpen`, and never asks chrome to run the
// focus-restoring popover dismiss.
//
// The SUT is loaded through an existence guard + import.meta.glob loader so this
// spec COLLECTS as a semantic red (every `it` fails on an `expect(...)`
// assertion, not a transform/resolution error) until the product module ships.

interface ChromeSlice {
  copilotPopoverOpen: boolean;
}

interface ActSurfaceSlice {
  importDialogOpen: boolean;
}

interface HandoffInput {
  chrome: ChromeSlice;
  actSurface: ActSurfaceSlice;
}

interface HandoffResult {
  chrome: ChromeSlice;
  actSurface: ActSurfaceSlice;
  /** Import owns focus: the transition must not restore focus to the ✧ trigger. */
  refocusCopilotTrigger: boolean;
}

interface TransitionModule {
  beginCopilotImportHandoff(input: HandoffInput): HandoffResult;
}

const moduleUrl = new URL('./copilotImportTransition.ts', import.meta.url);
const loaders = import.meta.glob('./copilotImportTransition.ts');
const loader = loaders['./copilotImportTransition.ts'];
const moduleExists = existsSync(fileURLToPath(moduleUrl)) && Boolean(loader);
const loaded = loader ? ((await loader()) as TransitionModule) : null;

function requireTransition(): TransitionModule {
  expect(
    moduleExists && loaded !== null,
    'Implement web/src/state/copilotImportTransition.ts exporting the pure '
      + 'beginCopilotImportHandoff cross-store transition (plan section 10).',
  ).toBe(true);
  return loaded as TransitionModule;
}

function handoff(input: HandoffInput): HandoffResult {
  return requireTransition().beginCopilotImportHandoff(input);
}

describe('copilot → import single-workspace handoff transition', () => {
  it('closes the Copilot popover and opens the single global import dialog', () => {
    const result = handoff({
      chrome: { copilotPopoverOpen: true },
      actSurface: { importDialogOpen: false },
    });
    expect(result.chrome.copilotPopoverOpen).toBe(false);
    expect(result.actSurface.importDialogOpen).toBe(true);
  });

  it('does not schedule Copilot-trigger refocus — import owns focus', () => {
    const result = handoff({
      chrome: { copilotPopoverOpen: true },
      actSurface: { importDialogOpen: false },
    });
    expect(result.refocusCopilotTrigger).toBe(false);
  });

  it('opens exactly one dialog even when import was already open', () => {
    const result = handoff({
      chrome: { copilotPopoverOpen: true },
      actSurface: { importDialogOpen: true },
    });
    expect(result.chrome.copilotPopoverOpen).toBe(false);
    expect(result.actSurface.importDialogOpen).toBe(true);
    expect(result.refocusCopilotTrigger).toBe(false);
  });

  it('is pure: it neither mutates its input nor returns the same references', () => {
    const input: HandoffInput = {
      chrome: { copilotPopoverOpen: true },
      actSurface: { importDialogOpen: false },
    };
    Object.freeze(input);
    Object.freeze(input.chrome);
    Object.freeze(input.actSurface);
    const result = handoff(input);
    // Frozen inputs would throw on mutation; a pure transition returns new slices.
    expect(input.chrome.copilotPopoverOpen).toBe(true);
    expect(input.actSurface.importDialogOpen).toBe(false);
    expect(result.chrome).not.toBe(input.chrome);
    expect(result.actSurface).not.toBe(input.actSurface);
  });

  it('is deterministic: repeated application yields an equal result', () => {
    const input: HandoffInput = {
      chrome: { copilotPopoverOpen: true },
      actSurface: { importDialogOpen: false },
    };
    const first = handoff(input);
    const second = handoff(input);
    expect(second).toEqual(first);
    // Applying it to its own output is a fixed point (copilot stays closed,
    // import stays open, focus stays with import).
    const settled = handoff({ chrome: first.chrome, actSurface: first.actSurface });
    expect(settled.chrome.copilotPopoverOpen).toBe(false);
    expect(settled.actSurface.importDialogOpen).toBe(true);
    expect(settled.refocusCopilotTrigger).toBe(false);
  });
});
