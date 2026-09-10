// The pure Copilot → Import single-workspace handoff.
//
// When the model decides the project needs data first, the Copilot import CTA
// runs this ONE cross-store transition: it closes the Copilot popover and opens
// the single global Import workspace dialog. It deliberately does NOT go through
// the popover's normal dismiss path, which schedules focus restoration to the ✧
// trigger and would race the import dialog for focus — Import owns focus, so the
// result carries an explicit `refocusCopilotTrigger: false` marker. The function
// is pure and deterministic: it returns fresh slices and never mutates its input.

export interface CopilotImportHandoffChromeSlice {
  copilotPopoverOpen: boolean;
}

export interface CopilotImportHandoffActSurfaceSlice {
  importDialogOpen: boolean;
}

export interface CopilotImportHandoffInput {
  chrome: CopilotImportHandoffChromeSlice;
  actSurface: CopilotImportHandoffActSurfaceSlice;
}

export interface CopilotImportHandoffResult {
  chrome: CopilotImportHandoffChromeSlice;
  actSurface: CopilotImportHandoffActSurfaceSlice;
  /** Import owns focus: the transition must not restore focus to the ✧ trigger. */
  refocusCopilotTrigger: boolean;
}

export function beginCopilotImportHandoff(
  input: CopilotImportHandoffInput,
): CopilotImportHandoffResult {
  return {
    chrome: { ...input.chrome, copilotPopoverOpen: false },
    actSurface: { ...input.actSurface, importDialogOpen: true },
    refocusCopilotTrigger: false,
  };
}
