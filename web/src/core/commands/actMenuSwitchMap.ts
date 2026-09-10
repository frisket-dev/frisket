// The Act-menu switch cases (ActMenuCommand, workbench/actSurface.ts,
// runActCommand's switch — useWorkspaceModel.tsx) mapped to their
// WorkspaceCommand home. Every key below is an ActMenuCommand the actSurface
// resolver actually emits (RIBBON_LAYOUT's fixed group commands) — a cleanup
// pass removed the eleven legacy-only rows ('export-root' no-op,
// view-grid/map/graph, toggle-discover, toggle-wrap, row-height, saved-views,
// provenance, help-docs, help-shortcuts) whose switch cases could never be
// reached because the shared Act model never emitted those values;
// reachability.test.ts now pins the key set to the emittable mirror so the
// genre cannot silently return.
//
// core/ may not import the workspace/ layer, so this table is a grep-verified
// mirror of runActCommand's real switch, not a live import of it — same
// convention every Handle/union mirror in this directory follows. Extracted to
// its own module (not left inline in registry.test.ts, where it originated) so
// it has exactly ONE runtime definition: registry.test.ts's own exhaustiveness
// assertions AND reachability.test.ts's discriminator both import THIS,
// instead of either a second hand-typed copy (drift risk) or one test file
// importing another test file's module — which re-registers that file's own
// describe/it blocks a second time under the importing suite (verified
// empirically: importing registry.test.ts directly doubled
// reachability.test.ts's reported test count).
import type { WorkspaceCommand } from './types';

export const RUN_ACT_SWITCH_CASE_TO_COMMAND: Readonly<
  Record<string, WorkspaceCommand['type']>
> = {
  import: 'import.open',
  sources: 'sources.open',
  'export-csv': 'export.open',
  'export-google-sheets': 'export.open',
  'export-column-tables': 'export.open',
  'ocr-compare': 'ocrCompare.open',
  'transcribe-compare': 'transcribeCompare.open',
  'translate-compare': 'translateCompare.open',
  'topic-compare': 'topicCompare.open',
};
