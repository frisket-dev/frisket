import type {
  GridFilterEntityValue,
  GridFilterSpec,
  GridSortSpec,
  ProjectInfo,
  SheetMeta,
} from '../api/open';

export interface WorkbenchHostContext {
  identity: {
    projectId: string;
    activeSheetId: string | null;
    routePath: string;
    activeRegion: string | null;
    activeContributionId: string | null;
  };
  selection: {
    selectedRowIds: string[];
    activeRowId: string | null;
    activeCell: { rowId: string; columnId: string } | null;
    activeColumnId: string | null;
    activeSourceId: string | null;
    activeEntityId: string | null;
    activeEvidenceLinkId: string | number | null;
  };
  gridState: {
    filter: GridFilterSpec | null;
    sort: GridSortSpec | null;
    lensId: number | null;
    columnOrder: string[];
    frozenColumns: number;
    /** Monotonic counter over "the rows behind this sheet changed" — the same
     *  `dataVersion` the grid's own row cache keys on (grid/useRowCache.ts),
     *  bumped when a run reaches a terminal status, on undo/redo, and after a
     *  column edit (state/jobStore.ts, useWorkspaceModel.tsx). A panel that
     *  READS derived data (not just grid rows) puts this in its fetch
     *  dependencies so it reloads when a run finishes instead of showing a
     *  confident number from before the run — the honest alternative to a
     *  poll loop of its own. */
    dataVersion: number;
  };
  /** grid.filter.applyBbox: applies the canonical geo-bbox grid filter
   *  ({columnName: {bbox: {min_lon, min_lat, max_lon, max_lat}}}) through the
   *  same dispatch the first-party MapView's "filter to this area" action
   *  uses (App.tsx applyGridBboxFilter) — one filter contract, no per-caller
   *  fork. columnId is resolved to the sheet's canonical filter key (column
   *  name) host-side. */
  gridFilter: {
    /** Replace the active grid filter with a complete multi-column spec.
     *  Friendly facets use this to OR checked values within one column and
     *  AND the resulting conditions across columns without racing a series
     *  of single-condition updates. */
    applySpec(filter: GridFilterSpec | null): void;
    applyBbox(columnId: string, bbox: [number, number, number, number]): void;
    /** grid.filter.applyValue: filter the active grid to a single column
     *  value through the same dispatch the column-header filter uses
     *  (App.tsx applyGridFilterForColumn). `eq` is the OpenRefine-style
     *  value facet (exact cell); `contains` is the mention-facet "filter to
     *  its rows" click (substring match on the column carrying the mention).
     *  columnId is resolved to the sheet's canonical filter key (column
     *  name) host-side; a column not on the active sheet is a no-op. */
    applyValue(columnId: string, value: string, operator?: 'eq' | 'contains'): void;
    /** grid.filter.applyEntity: filter the active grid to mentions in a marked
     *  entity-mentions JSON column — every mention of a canonical type, one
     *  exact raw spelling, or every spelling sharing one fingerprint. The
     *  payload is STRUCTURED, which is why this is its own capability rather
     *  than applyValue with a stringified argument. Single-select by
     *  construction: the applied spec is replaced, not merged. columnId is
     *  resolved to the sheet's canonical filter key (column name) host-side.
     *
     *  `valueLabel` is the spelling the caller's group is known by, carried so
     *  the toolbar chip can NAME the value: a fingerprint selector holds an
     *  internal comparison token nobody wrote, and printing it is never an
     *  option. Optional because only a caller holding the group has it — the
     *  chip falls back to naming the type alone rather than inventing one. */
    applyEntity(columnId: string, value: GridFilterEntityValue, valueLabel?: string): void;
    /** grid.filter.clear: drop the active column filter (the browse-loop
     *  "clear" affordance), same dispatch as the toolbar chip's clear. */
    clear(): void;
  };
  navigation: {
    openSheet(sheetId: string): void;
    openRow(sheetId: string | number, rowId: string | number): void;
    openColumn(columnId: string): void;
    openSource(sourceId: string | number): void;
    openEvidence(evidenceLinkId: string | number): void;
    openMap(columnId?: string): void;
    openGraph(anchorId?: string): void;
    openActionRoute(actionKind?: string): void;
    openMainViewContribution(contributionId: string): void;
  };
  actions: {
    runAction(actionKind: string): Promise<void>;
    previewAction(actionKind: string): Promise<void>;
    inspectProposal(proposalId: string): void;
    applyReviewDecision(reviewId: string, decision: 'accept' | 'reject'): Promise<void>;
    pollSource(sourceId: string | number): Promise<void>;
  };
  invalidation: {
    refreshSheets(): Promise<SheetMeta[] | void>;
    refreshHistory(): Promise<void>;
    refreshReviewCount(): Promise<void>;
    refreshGridRows(): void;
  };
  policy: {
    confirmCost(actionKind: string): Promise<boolean>;
    confirmEgress(target: string): Promise<boolean>;
    preferStaleEvidence: boolean;
  };
  layoutProfile: {
    persistPersonalOverride(contributionId: string, patch: Record<string, unknown>): void;
    persistProjectProfile(patch: Record<string, unknown>): Promise<void>;
    resetPersonalLayout(): void;
    resetProjectProfile(): Promise<void>;
    rewriteAlias(alias: string): string;
  };
}

function hostContextCapabilities(context: WorkbenchHostContext): string[] {
  const capabilities = [
    'project.identity',
    'sheet.active',
    'selection.rows',
    'grid.filter',
    'grid.sort',
    'grid.state.read',
    'grid.row.open',
    'grid.column.open',
    'grid.filter.applyBbox',
    'grid.filter.applySpec',
    'grid.filter.applyValue',
    'grid.filter.applyEntity',
    'grid.filter.clear',
    'projection.status',
    'host.navigation.openSheet',
    'host.navigation.openRow',
    'host.navigation.openColumn',
    'host.navigation.openSource',
    'host.navigation.openEvidence',
    'host.evidence.open',
    'host.navigation.openMap',
    'host.navigation.openGraph',
    'host.navigation.openActionRoute',
    'action.run',
    'action.preview',
    'source.list',
    'review.decision',
    'source.poll',
    'invalidation.sheets',
    'invalidation.history',
    'policy.costGate',
  ];
  if (!context.identity.activeSheetId) {
    return capabilities.filter((capability) => capability !== 'sheet.active');
  }
  return capabilities;
}

export function hostContextCapabilityAttribute(context: WorkbenchHostContext): string {
  return hostContextCapabilities(context).join(' ');
}

export function projectHostIdentity(
  project: ProjectInfo,
  activeSheetId: string | null,
  route: string,
): WorkbenchHostContext['identity'] {
  return {
    projectId: project.id,
    activeSheetId,
    routePath: route,
    activeRegion: null,
    activeContributionId: null,
  };
}
