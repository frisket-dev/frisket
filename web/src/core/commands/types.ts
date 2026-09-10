// The typed WorkspaceCommand discriminated union and the CommandContext it
// runs against. Framework-free (core/ boundary): this file MUST NOT import
// react/react-dom, state/, bind/, or any app layer. The React-facing adapter
// is bind/useCommand.ts; the palette adapter is the composition seam
// firstPartyCommandEntry in workbench/commandRegistry.ts.
//
// CommandContext is assembled INLINE in useWorkspaceModel.tsx, not via a
// separate bind/useCommandContext.ts, since it wraps ONE hook's local
// closures, not a per-project store instance. The `store` field below stays
// an unused Store<unknown> placeholder until a future stage needs it.

import type { Store } from '../store/types';

/**
 * Every first-party command as a distinct, typed member. Derived from two
 * call-site families in useWorkspaceModel.tsx:
 *   - hostContext.navigation openers
 *   - the runActCommand switch + the palette openers
 *
 * Nine members whose ONLY dispatch site was an unreachable runActCommand
 * switch case (view.grid/map/graph, discover.toggle, wrap.toggle,
 * rowHeight.cycle, savedViews.toggle, provenance.toggle, palette.open) were
 * deleted, the same disposition the reachability discriminator
 * (reachability.test.ts) earlier forced for `workView.set`. Their FEATURES
 * all live via other paths (toolbar wrap button, ⋯ overflow menu, ⌘K palette
 * via chrome.openCommandPalette, WorkViewSwitcher) — only the
 * never-dispatched command indirection was removed.
 */
export type WorkspaceCommand =
  // navigation openers (hostContext.navigation)
  | { type: 'openSheet'; sheetId: string }
  | { type: 'openRow'; sheetId: string; rowId: string; columnId?: string }
  | { type: 'openColumn'; columnId: string }
  | { type: 'openSource'; sourceId: string }
  | { type: 'openEvidence'; linkId: string | number; host?: 'modalOrPeek' | 'mainView' }
  | { type: 'openMap'; columnId?: string }
  | { type: 'openGraph' }
  | { type: 'openActionRoute'; actionKind?: string }
  | { type: 'openMainViewContribution'; contributionId: string }
  // runActCommand cases
  | { type: 'import.open' }
  | { type: 'sources.open' }
  | { type: 'export.open'; kind: 'dataset' | 'google_sheets' | 'column_tables' }
  | { type: 'ocrCompare.open' }
  | { type: 'transcribeCompare.open' }
  | { type: 'translateCompare.open' }
  | { type: 'topicCompare.open' }
  // palette opener
  | { type: 'settings.open' };

// ---- CommandContext ---------------------------------------------------
//
// The handles a first-party command's run() may touch — assembled in bind/
// from the per-project store handles, never a mega-bag.

export interface RouteHandle {
  readonly store: Store<unknown>;
  /** = selectSheet */
  openSheet(sheetId: string): void;
  /** = openRowRef; columnId is accepted for the command's shape parity but
   *  ignored, matching openRowRef's own 2-arg signature (today's
   *  hostContext.navigation.openRow drops it too). */
  openRow(sheetId: string, rowId: string, columnId?: string): void;
  /** = navigation.openColumn's inline body */
  openColumn(columnId: string): void;
  /** = navigation.openSource's inline body */
  openSource(sourceId: string): void;
  /** = navigation.openActionRoute's inline body */
  openActionRoute(actionKind?: string): void;
}
export interface GridViewHandle {
  // toggleWrap/cycleRowHeight removed with the dead wrap.toggle/rowHeight.cycle
  // commands — the live paths are the toolbar wrap button and the ⋯
  // overflow's row-height select, which call the hook's own composed
  // closures directly.
  readonly store: Store<unknown>;
}
export interface ChromeHandle {
  readonly store: Store<unknown>;
  /** = openEvidenceViewerForWorkspace */
  openEvidence(linkId: string | number, host?: 'modalOrPeek' | 'mainView'): void;
  /** = navigation.openMap's inline body */
  openMap(columnId?: string): void;
  /** = openGraphPanel */
  openGraph(): void;
  /** = navigation.openMainViewContribution's inline body */
  openMainViewContribution(contributionId: string): void;
  /** = openSourcesFromCommandPalette — the canonical body for BOTH the
   *  palette entry AND runActCommand's 'sources' case (superset-body
   *  divergence, see registry.ts). */
  openSources(): void;
  /** = openSettingsFromCommandPalette */
  openSettings(): void;
}
export interface ScratchHandle {
  readonly store: Store<unknown>;
  /** = openOcrCompareTab + recordCommandAction, the workbenchCommandEntries
   *  OCR_COMPARE_COMMAND_DESCRIPTOR body — the canonical superset body, see
   *  registry.ts. */
  openOcrCompare(): void;
  /** = openTranscribeCompareTab + recordCommandAction, the
   *  TRANSCRIBE_COMPARE_COMMAND_DESCRIPTOR body. */
  openTranscribeCompare(): void;
  /** = openTranslateCompareTab + recordCommandAction, the
   *  TRANSLATE_COMPARE_COMMAND_DESCRIPTOR body. */
  openTranslateCompare(): void;
  /** Opens the ephemeral transcript topic-segmentation bake-off. */
  openTopicCompare(): void;
}
export interface ActSurfaceHandle {
  readonly store: Store<unknown>;
  /** = openImportDialog */
  openImportDialog(): void;
  /** = setActExportModal */
  setExportModal(kind: 'dataset' | 'google_sheets' | 'column_tables'): void;
}
export interface JobsHandle {
  readonly store: Store<unknown>;
}
export interface SelectionHandle {
  readonly store: Store<unknown>;
}

/** Assembled in bind/useCommandContext; one destructured object, seven
 *  handle families (what collapses from 18 params is param GROUPS bundled
 *  here, not a literal count of three parameters). */
export interface CommandContext {
  route: RouteHandle;
  grid: GridViewHandle;
  chrome: ChromeHandle;
  scratch: ScratchHandle;
  actSurface: ActSurfaceHandle;
  jobs: JobsHandle;
  selection: SelectionHandle;
}

// ---- CommandDescriptor -----------------------------------------------------
//
// Lives here (not registry.ts) so a per-command file under
// core/commands/first-party/*.command.ts can import it without creating a
// type-only import cycle with registry.ts, which glob-discovers those same
// files.

export interface CommandDescriptor<C extends WorkspaceCommand = WorkspaceCommand> {
  /** Discriminant this descriptor handles; equals its key in the table. */
  match: C['type'];
  /** Stable test hook, mirrors the current data-testid conventions. */
  testId: string;
  /** Pure predicate over current snapshots; drives disabled/omitted UI.
   *  Every entry stays `() => true` — see registry.ts's file-level comment
   *  for why the real gates stay inline in run(), unchanged by the glob
   *  conversion. */
  enabled(ctx: CommandContext): boolean;
  /** The command's effect: the verbatim callback body, retargeted to ctx
   *  handles instead of closures. */
  run(command: C, ctx: CommandContext): void | Promise<void>;
}

/** Narrows CommandDescriptor to one union member — the shape every
 *  first-party/*.command.ts file's default export is typed as, so its run()
 *  sees ONLY its own command's fields (e.g. openSheet.command.ts's run sees
 *  `{type:'openSheet'; sheetId: string}`, not the full union). */
export type DescriptorFor<K extends WorkspaceCommand['type']> = CommandDescriptor<
  Extract<WorkspaceCommand, { type: K }>
>;
