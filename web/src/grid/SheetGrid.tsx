import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type MutableRefObject,
} from 'react';
import { Maximize2 } from 'lucide-react';
import {
  DataEditor,
  CompactSelection,
  GridCellKind,
  GridColumnIcon,
  GridColumnMenuIcon,
  type DataEditorRef,
  type EditableGridCell,
  type GridCell,
  type CellClickedEventArgs,
  type GridColumn,
  type GridSelection,
  type GridMouseGroupHeaderEventArgs,
  type GroupHeaderClickedEventArgs,
  type Item,
  type Rectangle,
  type Theme,
} from '@glideapps/glide-data-grid';
import '@glideapps/glide-data-grid/dist/index.css';
import {
  type SavedViewColumnGroup,
  type CellValue,
  type ColumnDef,
  type GridFilterSpec,
  type GridSortSpec,
  type GridSortDirection,
  type PreviewOverlayCell,
  type PreviewOverlayColumn,
  type PreviewCellDetail,
  type Row,
  type RunProgress,
  type SheetDataOptions,
  type SheetMeta,
} from '../api/open';
import type { ProjectApiPort } from '../api/ports';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import {
  audioMedia,
  buildCell,
  cellEditability,
  customRenderers,
  isExpandableEntityMentionCell,
  headerIcons,
  isOverlayEditable,
  oneClickFacetValue,
  isAudioPlayButtonHit,
  shouldOpenDrawerOnKey,
  typeHeaderIcon,
  withOneClickFacetCursor,
  withAudioPlayback,
} from './cells';
import { countLabel } from '../format';
import { ColumnAnnotationChip, type ColumnAnnotation } from './columnAnnotations';
import { readAiColumnTheme, readGridCellPalette, readGridTheme, type GridCellPalette } from './gridTheme';
import {
  addColumnScrollGutter,
  resolveColumnAnnotationPlacement,
} from './gridHeaderBand';
import { useRowCache, type RowCache } from './useRowCache';
import { createRowCacheStore, resolveRowCacheScope, type RowCacheStoreHandle } from './rowCacheStore';
import { isActiveRunStatus, isStreamingRunStatus } from '../runStatusModel';
import { usePoll } from '../hooks/usePoll';
import { PanelLoading } from '../components/PanelPrimitives';
import { createAudioPlaybackSource } from '../state/audioPlaybackStore';
import { previewGridCell } from './previewCells';
import {
  consumeGridCellReveal,
  getPendingGridCellReveal,
  subscribeGridCellReveal,
} from './gridCellReveal';

// Include projectId because sheet ids repeat across projects.
const widthsKey = (storageScope: string, sheetId: string) =>
  `frisket:col-widths:${storageScope}:${sheetId}`;
const legacyWidthsKey = (sheetId: string) => `frisket:col-widths:${sheetId}`;
const EMPTY_COLUMN_NAMES: string[] = [];
const EMPTY_COLUMN_ANNOTATIONS: ColumnAnnotation[] = [];
const GROUP_HEADER_HEIGHT = 28;

export type ColumnGroupViewSpec = SavedViewColumnGroup;

interface ColumnGroupLayoutState {
  label?: string;
  showConfidence: boolean;
  showJustification: boolean;
}

interface ColumnGroupModel {
  runId: string;
  groupKey: string;
  columns: string[];
  baseColumns: string[];
  confidenceColumns: string[];
  justificationColumns: string[];
  fallbackLabel: string;
}

interface NativeColumnGroupsState {
  groups: ColumnGroupModel[];
  groupByRunId: Map<string, ColumnGroupModel>;
  groupByKey: Map<string, ColumnGroupModel>;
  layouts: Record<string, ColumnGroupLayoutState>;
  setGroupLayout(runId: string, patch: Partial<ColumnGroupLayoutState>): void;
}

interface GroupMenuState {
  runId: string;
  bounds: Rectangle;
}

interface ColumnGroupChangeSink {
  notify(groups: ColumnGroupViewSpec[]): void;
}

function loadColWidths(storageScope: string, sheetId: string): Record<string, number> {
  try {
    const raw = localStorage.getItem(widthsKey(storageScope, sheetId));
    if (raw) return JSON.parse(raw) as Record<string, number>;
    // One-time migration: adopt the pre-scoping key, then retire it so the
    // next project with a colliding sheet id can't inherit these widths.
    const legacy = localStorage.getItem(legacyWidthsKey(sheetId));
    if (legacy) {
      localStorage.setItem(widthsKey(storageScope, sheetId), legacy);
      localStorage.removeItem(legacyWidthsKey(sheetId));
      return JSON.parse(legacy) as Record<string, number>;
    }
    return {};
  } catch {
    return {};
  }
}

const columnGroupsStorageKey = (storageScope: string, sheetId: string) =>
  `frisket:column-groups:${storageScope}:${sheetId}`;

function loadColumnGroupLayouts(
  storageScope: string,
  sheetId: string,
): Record<string, ColumnGroupLayoutState> {
  try {
    const raw = localStorage.getItem(columnGroupsStorageKey(storageScope, sheetId));
    return raw ? (JSON.parse(raw) as Record<string, ColumnGroupLayoutState>) : {};
  } catch {
    return {};
  }
}

function persistColumnGroupLayouts(
  storageScope: string,
  sheetId: string,
  layouts: Record<string, ColumnGroupLayoutState>,
) {
  localStorage.setItem(columnGroupsStorageKey(storageScope, sheetId), JSON.stringify(layouts));
}

function isConfidenceColumn(name: string): boolean {
  return name === 'confidence' || name.endsWith('_confidence');
}

function isJustificationColumn(name: string): boolean {
  return name === 'justification' || name.endsWith('_justification');
}

function columnGroupKey(runId: string): string {
  return `ai-run:${runId}`;
}

function resolveColumnGroupLayout(
  group: ColumnGroupModel,
  layouts: Record<string, ColumnGroupLayoutState>,
): Required<ColumnGroupLayoutState> {
  const layout = layouts[group.runId];
  return {
    label: layout?.label || group.fallbackLabel,
    showConfidence: layout?.showConfidence ?? true,
    showJustification: layout?.showJustification ?? true,
  };
}

function buildColumnGroupSpecs(
  groups: ColumnGroupModel[],
  layouts: Record<string, ColumnGroupLayoutState>,
): ColumnGroupViewSpec[] {
  return groups.map((group) => {
    const layout = resolveColumnGroupLayout(group, layouts);
    return {
      run_id: Number(group.runId),
      label: layout.label,
      columns: group.columns,
      show_confidence: layout.showConfidence,
      show_justification: layout.showJustification,
    };
  });
}

function buildColumnGroups(columns: ColumnDef[]): ColumnGroupModel[] {
  const byRun = new Map<string, ColumnGroupModel>();
  for (const col of columns) {
    if (!col.ai || !col.latestRunId) continue;
    const runId = String(col.latestRunId);
    const group = byRun.get(runId) ?? {
      runId,
      groupKey: columnGroupKey(runId),
      columns: [],
      baseColumns: [],
      confidenceColumns: [],
      justificationColumns: [],
      fallbackLabel: col.ai.actionName || 'AI run',
    };
    group.columns.push(col.name);
    if (isConfidenceColumn(col.name)) {
      group.confidenceColumns.push(col.name);
    } else if (isJustificationColumn(col.name)) {
      group.justificationColumns.push(col.name);
    } else {
      group.baseColumns.push(col.name);
    }
    byRun.set(runId, group);
  }
  return [...byRun.values()].filter((group) => group.columns.length > 1);
}

function columnGroupSpecsKey(groups: ColumnGroupViewSpec[]): string {
  return JSON.stringify(groups);
}

function useColumnGroupChangeSink(
  onChange: ((groups: ColumnGroupViewSpec[]) => void) | undefined,
): ColumnGroupChangeSink {
  const onChangeRef = useRef(onChange);
  const lastSpecsKey = useRef<string | null>(null);

  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  return useMemo(
    () => ({
      notify(groups: ColumnGroupViewSpec[]) {
        const nextKey = columnGroupSpecsKey(groups);
        if (lastSpecsKey.current === nextKey) return;
        lastSpecsKey.current = nextKey;
        onChangeRef.current?.(groups);
      },
    }),
    [],
  );
}

/** The sheet derived FROM this sheet, if any — drives the child-count chip. */
export interface ChildSheetRef {
  id: string;
  name: string;
}

/** Show only the rows derived from one parent row (chip click-through). */
export interface ParentRowFilter {
  parentRowId: string;
  /** The parent row's childCount — the filtered sheet's row total. */
  count: number;
}

export interface SheetGridProps {
  sheet: SheetMeta;
  /** Bump to hard-invalidate the cache (undo/redo, review edits). */
  dataVersion: number;
  /** Progress of a run touching this sheet, for soft refresh while it streams. */
  liveRun: RunProgress | null;
  /** Global row height (Google-Sheets select-all style: every row alike). */
  rowHeight: number;
  /** Wrap long text in text cells (pairs with taller rows). */
  wrapText: boolean;
  /** When set, a trailing chip column shows each row's derived-row count. */
  childSheet?: ChildSheetRef | null;
  /** When set, the grid shows only one parent row's derived children. */
  parentRowFilter?: ParentRowFilter | null;
  /** True while row details are visible; selection should then update them. */
  rowDrawerOpen?: boolean;
  /** Number of visible left-prefix columns frozen in place. */
  frozenColumnCount?: number;
  activeFilter?: GridFilterSpec | null;
  activeSort?: GridSortSpec | null;
  columnOrder?: string[] | null;
  /** Lens-view: page EXACTLY these row ids in ranked order. */
  lensRowIds?: number[] | null;
  /** Per-row {distance, score} for the lens view, keyed by row id — rendered as
   *  trailing result columns so the ranking metric is VISIBLE in the grid. */
  lensScores?: Record<string, { distance: number | null; score: number | null }> | null;
  /** In-memory action preview: the sampled output columns. Brand-new outputs
   *  append as trailing virtual columns; an output whose name matches a real
   *  column overlays that column's cells in place. */
  previewColumns?: PreviewOverlayColumn[] | null;
  /** Preview sample values: rowId (string) -> output field name -> cell. Overlaid
   *  in getCellContent for the sampled rows (never written to the sheet). */
  previewCells?: Record<string, Record<string, PreviewOverlayCell>> | null;
  columnGroupsStorageScope: string;
  columnGroupsVersion?: number;
  hiddenColumnNames?: string[];
  /** The USER-hidden columns (Google-Sheets-style collapse), a subset of
   *  hiddenColumnNames. Drives the boundary revive chevrons; the AI column-group
   *  justification/confidence hides are excluded (they revive via group toggles). */
  userHiddenColumnNames?: string[];
  /** Revive a hidden run (the whole run revives together). */
  onRevealColumns?(columns: string[]): void;
  /** Open the add-column popover anchored at the header "+" affordance. */
  onAddColumnAtEnd?(anchor: { x: number; y: number }): void;
  onRowOpen(row: Row, col?: ColumnDef, preview?: PreviewCellDetail | null): void;
  onColumnOpen(col: ColumnDef): void;
  /** Header LABEL click → cycle server-backed sort for this column (asc→desc).
   *  When supplied it takes over the header click from onColumnOpen (which stays
   *  reachable via the header menu's Column settings). */
  onHeaderSort?(col: ColumnDef): void;
  onColumnHeaderMenu?(request: { column: ColumnDef; columnIndex: number; bounds: Rectangle }): void;
  onColumnGroupsChange?(groups: ColumnGroupViewSpec[]): void;
  onSelectedRowsChange?(selection: { sheetId: string; rowIds: string[]; rowIndexes: number[] }): void;
  onColumnOrderChange?(columns: string[]): void;
  onCellEdit(row: Row, col: ColumnDef, value: CellValue): Promise<void>;
  /** Registry-declared facet cell click → the same exact-value filter
   *  transition used by the Facets panel and column-header filter. */
  onFacetValueFilter?(
    col: ColumnDef,
    value: string,
    operator: 'eq' | 'contains',
  ): void;
  /** The shared row-cache registry, keyed by sheetId, owned by the workspace
   *  layer (bind/useRowCacheHandle) and passed down so the cache SURVIVES a
   *  SheetGrid remount (split toggle, Document/Answers view swap) instead of
   *  restarting, and the toolbar can read the SAME slot's rowCount directly
   *  via useSyncExternalStore (useWorkspaceModel.tsx's
   *  activeRowCacheKey/visibleRowCount) — no more push-up effect. Optional: a
   *  standalone render (tests, Storybook) falls back to a private, per-mount
   *  registry for that case. */
  rowCacheStore?: RowCacheStoreHandle;
  /** EXTERNAL header annotations keyed by column ids (arbitrary message +
   *  optional progress), merged with the grid's own run-created "New columns"
   *  annotation. The reusable surface the future 'imported the videos, hid X
   *  columns' case rides on. */
  columnAnnotations?: ColumnAnnotation[];
}

// While a run creates output columns, keep a "New columns" annotation over
// them; hold it briefly past terminal so it does not vanish the instant the
// last row lands, then clear (or on click-dismiss).
const NEW_COLUMNS_GRACE_MS = 2500;

const CHILD_CHIP_COL = '__child_chip';
// Lens-view result columns: the ranking metric, surfaced as real grid
// columns so distance/score are VISIBLE in the grid (not raw vectors, not
// only the sidebar). Appended only while a lens view is active.
const LENS_DISTANCE_COL = '__lens_distance';
const LENS_SCORE_COL = '__lens_score';
// Trailing virtual columns for an in-memory action preview: a brand-new sampled
// output that doesn't overwrite an existing column is shown as an appended
// preview column (id prefixed so it never collides with a real column name).
const PREVIEW_COL_PREFIX = '__preview::';

const emptyGridSelection = (): GridSelection => ({
  columns: CompactSelection.empty(),
  rows: CompactSelection.empty(),
});

/** "3 faces" / "1 face" / "2 people" (not "2 peoples") from the child
 *  sheet's name — irregular-aware inflection via format.ts's countLabel. */
function childChipLabel(n: number, childSheetName: string): string {
  return countLabel(n, childSheetName.trim().toLowerCase(), 'rows');
}

function editableValue(cell: EditableGridCell): CellValue | undefined {
  switch (cell.kind) {
    case GridCellKind.Text:
    case GridCellKind.Markdown:
    case GridCellKind.Uri:
      return cell.data;
    case GridCellKind.Number:
      return cell.data ?? null;
    case GridCellKind.Boolean:
      return typeof cell.data === 'boolean' ? cell.data : null;
    default:
      return undefined;
  }
}

/** A revive affordance sitting on the boundary AFTER the nearest visible column
 *  to the left of a user-hidden run (Google-Sheets-style). `x` is the canvas
 *  x-offset of that boundary before horizontal scroll is applied. */
interface HiddenColumnBoundary {
  key: string;
  x: number;
  columns: string[];
}

interface ColumnModel {
  columns: GridColumn[];
  gridColumns: ColumnDef[];
  gridTheme: Partial<Theme>;
  aiColumnTheme: Partial<Theme>;
  gridCellPalette: GridCellPalette;
  onColumnResize(column: GridColumn, newSize: number): void;
  /** Boundaries between visible columns that flank a user-hidden run. */
  hiddenBoundaries: HiddenColumnBoundary[];
  /** Canvas x-offset just past the last visible data column — where the "+"
   *  add-column affordance sits at the right end of the header row. */
  columnsEndX: number;
  /** Canvas x-offset just past each visible column's right edge, by name —
   *  the geometry the header overlay band positions per-column DOM affordances
   *  against (the revive-chevron/add-column affordances) — the one source of
   *  truth for column x-offsets. */
  rightEdgeByName: Map<string, number>;
}

// Glide's row-marker gutter width (rowMarkers width below); the header overlay
// band offsets its affordances past it so they align with the canvas columns.
const ROW_MARKER_WIDTH = 40;
// Horizontal gap that pushes the header "+" add-column button clear of the
// last column's right edge, which the button is centred on.
const ADD_COLUMN_BUTTON_GAP = 34;

function useColumnModel(
  sheet: SheetMeta,
  storageScope: string,
  columnOrder: string[] | null | undefined,
  hiddenColumnNames: string[],
  userHiddenColumnNames: string[],
  childSheet: ChildSheetRef | null | undefined,
  columnGroups: NativeColumnGroupsState,
  lensActive: boolean,
  previewNewColumns: PreviewOverlayColumn[],
  activeSort: GridSortSpec | null | undefined,
): ColumnModel {
  const stored = useMemo(() => loadColWidths(storageScope, sheet.id), [storageScope, sheet.id]);
  const [colWidthsBySheet, setColWidthsBySheet] = useState<Record<string, Record<string, number>>>({});
  const colWidths = colWidthsBySheet[sheet.id] ?? stored;
  const hiddenNames = useMemo(() => new Set(hiddenColumnNames), [hiddenColumnNames]);
  const orderedSheetColumns = useMemo(() => {
    if (!columnOrder?.length) return sheet.columns;
    const byName = new Map(sheet.columns.map((col) => [col.name, col]));
    const ordered: ColumnDef[] = [];
    const seen = new Set<string>();
    for (const name of columnOrder) {
      const col = byName.get(name);
      if (!col) continue;
      ordered.push(col);
      seen.add(col.name);
    }
    return [...ordered, ...sheet.columns.filter((col) => !seen.has(col.name))];
  }, [columnOrder, sheet.columns]);
  const gridColumns = useMemo(
    () => orderedSheetColumns.filter((col) => !hiddenNames.has(col.name)),
    [hiddenNames, orderedSheetColumns],
  );

  const onColumnResize = useCallback(
    (column: GridColumn, newSize: number) => {
      if (!column.id) return;
      setColWidthsBySheet((bySheet) => {
        const next = {
          ...(bySheet[sheet.id] ?? stored),
          [column.id as string]: Math.round(newSize),
        };
        localStorage.setItem(widthsKey(storageScope, sheet.id), JSON.stringify(next));
        return { ...bySheet, [sheet.id]: next };
      });
    },
    [sheet.id, storageScope, stored],
  );

  // Grid + AI-column themes are derived from the CSS token palette (styles.css
  // :root) so the canvas grid cannot drift from CSS, and re-derived when the
  // active `data-frisket-theme` flips. The live base theme is published on
  // window.__frisketGridTheme (dev/test builds only) so the reskin e2e can
  // drift-guard it — not a production API surface.
  const [gridTheme, setGridTheme] = useState<Partial<Theme>>(readGridTheme);
  const [aiColumnTheme, setAiColumnTheme] = useState<Partial<Theme>>(readAiColumnTheme);
  const [gridCellPalette, setGridCellPalette] = useState<GridCellPalette>(readGridCellPalette);
  useEffect(() => {
    const sync = () => {
      const next = readGridTheme();
      setGridTheme(next);
      setAiColumnTheme(readAiColumnTheme());
      setGridCellPalette(readGridCellPalette());
      if (import.meta.env.DEV) {
        (window as unknown as { __frisketGridTheme?: Partial<Theme> }).__frisketGridTheme = next;
      }
    };
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-frisket-theme'],
    });
    return () => observer.disconnect();
  }, []);

  const sortByName = useMemo(() => {
    const map = new Map<string, GridSortDirection>();
    for (const rule of activeSort ?? []) map.set(rule.column, rule.dir);
    return map;
  }, [activeSort]);
  const columns = useMemo<GridColumn[]>(() => {
    const cols: GridColumn[] = gridColumns.map((c) => {
      // Header LABEL click cycles sort; the active-sort arrow rides on the title
      // (Glide draws headers on canvas, so this is the on-canvas arrow).
      const dir = sortByName.get(c.name);
      const title = dir ? `${c.name} ${dir === 'asc' ? '↑' : '↓'}` : c.name;
      // Icon priority (the funnel-when-filtered sprite was dropped — filtered
      // state now renders in the caret menu): AI columns keep their bolt;
      // else the column's type-tag glyph.
      const icon = c.ai ? 'aiBolt' : typeHeaderIcon(c.type);
      return {
        id: c.id,
        title,
        width: colWidths[c.id] ?? c.width ?? 140,
        ...(c.latestRunId && columnGroups.groupByRunId.has(String(c.latestRunId))
          ? { group: columnGroups.groupByRunId.get(String(c.latestRunId))?.groupKey }
          : {}),
        hasMenu: true,
        menuIcon: GridColumnMenuIcon.Dots,
        ...(icon ? { icon } : {}),
        ...(c.ai ? { themeOverride: aiColumnTheme } : {}),
      };
    });
    // Trailing chip column: each row's derived-row count in the child sheet
    // ("3 faces"); clicking filters the child sheet to that row's children.
    if (childSheet) {
      cols.push({
        id: CHILD_CHIP_COL,
        title: `↳ ${childSheet.name}`,
        width: colWidths[CHILD_CHIP_COL] ?? 130,
      });
    }
    // Lens-view result columns: the per-row ranking metric, made VISIBLE in the
    // grid as real trailing columns (distance then score).
    if (lensActive) {
      cols.push({
        id: LENS_DISTANCE_COL,
        title: '⌁ distance',
        width: colWidths[LENS_DISTANCE_COL] ?? 110,
      });
      cols.push({
        id: LENS_SCORE_COL,
        title: '⌁ score',
        width: colWidths[LENS_SCORE_COL] ?? 110,
      });
    }
    // Preview: brand-new sampled outputs (no existing column to overwrite) append
    // as trailing virtual columns. Overwrites reuse their real column in place.
    for (const previewCol of previewNewColumns) {
      const id = `${PREVIEW_COL_PREFIX}${previewCol.name}`;
      cols.push({
        id,
        title: `◑ ${previewCol.name}`,
        width: colWidths[id] ?? 160,
        themeOverride: aiColumnTheme,
      });
    }
    return cols;
  }, [gridColumns, colWidths, childSheet, columnGroups.groupByRunId, lensActive, aiColumnTheme, previewNewColumns, sortByName]);

  // The revive chevrons + the add-column "+" ride a DOM band over the canvas
  // header (glide has no per-boundary DOM). A hidden run is a maximal sequence
  // of USER-hidden columns in the active order; its chevron sits on the boundary
  // just past the nearest visible column to its left (or the grid's left edge).
  const { hiddenBoundaries, columnsEndX, rightEdgeByName } = useMemo(() => {
    const visibleNames = new Set(gridColumns.map((col) => col.name));
    const rightEdgeByName = new Map<string, number>();
    let x = ROW_MARKER_WIDTH;
    for (const col of gridColumns) {
      x += colWidths[col.id] ?? col.width ?? 140;
      rightEdgeByName.set(col.name, x);
    }
    const endX = x;
    const userHidden = new Set(userHiddenColumnNames);
    const boundaries: HiddenColumnBoundary[] = [];
    let lastVisibleName: string | null = null;
    let run: string[] = [];
    const flush = () => {
      if (run.length === 0) return;
      const boundaryX = lastVisibleName
        ? rightEdgeByName.get(lastVisibleName) ?? ROW_MARKER_WIDTH
        : ROW_MARKER_WIDTH;
      boundaries.push({
        key: `${lastVisibleName ?? '<start>'}:${run.join(',')}`,
        x: boundaryX,
        columns: run,
      });
      run = [];
    };
    for (const col of orderedSheetColumns) {
      if (userHidden.has(col.name)) {
        run.push(col.name);
      } else if (visibleNames.has(col.name)) {
        flush();
        lastVisibleName = col.name;
      }
      // A group-hidden column (justification/confidence) is neither rendered nor
      // user-hidden; skip it without breaking an adjacent user-hidden run.
    }
    flush();
    return { hiddenBoundaries: boundaries, columnsEndX: endX, rightEdgeByName };
  }, [colWidths, gridColumns, orderedSheetColumns, userHiddenColumnNames]);

  return {
    columns,
    gridColumns,
    gridTheme,
    aiColumnTheme,
    gridCellPalette,
    onColumnResize,
    hiddenBoundaries,
    columnsEndX,
    rightEdgeByName,
  };
}

function useNativeColumnGroups(
  sheet: SheetMeta,
  storageScope: string,
  version: number | undefined,
  changeSink: ColumnGroupChangeSink,
  api: ProjectApiPort,
): NativeColumnGroupsState {
  const groups = useMemo(() => buildColumnGroups(sheet.columns), [sheet.columns]);
  const groupByRunId = useMemo(
    () => new Map(groups.map((group) => [group.runId, group] as const)),
    [groups],
  );
  const groupByKey = useMemo(
    () => new Map(groups.map((group) => [group.groupKey, group] as const)),
    [groups],
  );
  const [layouts, setLayouts] = useState<Record<string, ColumnGroupLayoutState>>(() =>
    loadColumnGroupLayouts(storageScope, sheet.id),
  );
  const layoutsRef = useRef(layouts);
  const labelFetchState = useRef<{
    sheetId: string;
    fetched: Set<string>;
    inflight: Set<string>;
    attempts: Map<string, number>;
  }>({
    sheetId: sheet.id,
    fetched: new Set(),
    inflight: new Set(),
    attempts: new Map(),
  });

  const commitLayouts = useCallback(
    (next: Record<string, ColumnGroupLayoutState>) => {
      layoutsRef.current = next;
      persistColumnGroupLayouts(storageScope, sheet.id, next);
      setLayouts(next);
    },
    [sheet.id, storageScope],
  );

  const publishLayouts = useCallback(
    (next: Record<string, ColumnGroupLayoutState>, specsGroups: ColumnGroupModel[] = groups) => {
      changeSink.notify(buildColumnGroupSpecs(specsGroups, next));
    },
    [changeSink, groups],
  );

  useEffect(() => {
    const next = loadColumnGroupLayouts(storageScope, sheet.id);
    layoutsRef.current = next;
    labelFetchState.current = {
      sheetId: sheet.id,
      fetched: new Set(),
      inflight: new Set(),
      attempts: new Map(),
    };
    // Alive-guarded: a microtask queued by a mount that immediately unmounts
    // — or a fast sheet switch — must not fire setState/notify with the
    // stale sheet's layouts.
    let alive = true;
    queueMicrotask(() => {
      if (!alive) return;
      setLayouts(next);
      changeSink.notify(buildColumnGroupSpecs(groups, next));
    });
    return () => {
      alive = false;
    };
  }, [changeSink, groups, sheet.id, storageScope, version]);

  useEffect(() => {
    changeSink.notify(buildColumnGroupSpecs(groups, layouts));
  }, [changeSink, groups, layouts]);

  const seedColumnByRunId = useMemo(() => {
    const byRun = new Map<string, ColumnDef>();
    for (const col of sheet.columns) {
      if (col.ai && col.latestRunId != null && !byRun.has(String(col.latestRunId))) {
        byRun.set(String(col.latestRunId), col);
      }
    }
    return byRun;
  }, [sheet.columns]);

  useEffect(() => {
    let alive = true;
    const requested = new Set<string>();
    const scheduled = new Set<number>();
    const state = labelFetchState.current;
    const fetchLabelForGroup = (group: ColumnGroupModel, seed: ColumnDef) => {
      const retryLabel = () => {
        const attempts = (state.attempts.get(group.runId) ?? 0) + 1;
        state.attempts.set(group.runId, attempts);
        if (attempts >= 5) {
          state.fetched.add(group.runId);
          return;
        }
        window.setTimeout(() => {
          if (alive) fetchLabelForGroup(group, seed);
        }, 250);
      };

      state.inflight.add(group.runId);
      requested.add(group.runId);
      void api
        .getColumnRuns(seed.id)
        .then((info) => {
          state.inflight.delete(group.runId);
          if (!alive) return;
          let run = info.runs[0];
          for (const candidate of info.runs) {
            if (candidate.current) {
              run = candidate;
              break;
            }
          }
          const label =
            typeof run?.spec?.group_label === 'string' && run.spec.group_label.trim()
              ? run.spec.group_label.trim()
              : run?.actionKind || group.fallbackLabel;
          if (label === group.fallbackLabel) {
            retryLabel();
            return;
          }
          state.attempts.delete(group.runId);
          state.fetched.add(group.runId);
          const current = layoutsRef.current[group.runId];
          if (current?.label && current.label !== group.fallbackLabel) return;
          commitLayouts({
            ...layoutsRef.current,
            [group.runId]: {
              showConfidence: current?.showConfidence ?? true,
              showJustification: current?.showJustification ?? true,
              label,
            },
          });
        })
        .catch(() => {
          state.inflight.delete(group.runId);
          if (!alive) return;
          retryLabel();
        });
    };

    for (const group of groups) {
      const layout = layoutsRef.current[group.runId];
      if (layout?.label && layout.label !== group.fallbackLabel) continue;
      if (state.fetched.has(group.runId) || state.inflight.has(group.runId)) continue;
      const seed = seedColumnByRunId.get(group.runId);
      if (!seed) continue;
      state.inflight.add(group.runId);
      requested.add(group.runId);
      const timer = window.setTimeout(() => {
        scheduled.delete(timer);
        if (alive) fetchLabelForGroup(group, seed);
      }, 0);
      scheduled.add(timer);
    }
    return () => {
      alive = false;
      for (const timer of scheduled) {
        window.clearTimeout(timer);
      }
      for (const runId of requested) {
        state.inflight.delete(runId);
      }
    };
  }, [commitLayouts, groups, seedColumnByRunId]);

  const setGroupLayout = useCallback(
    (runId: string, patch: Partial<ColumnGroupLayoutState>) => {
      const current = layoutsRef.current[runId] ?? {
        showConfidence: true,
        showJustification: true,
      };
      const next = {
        ...layoutsRef.current,
        [runId]: { ...current, ...patch },
      };
      commitLayouts(next);
      publishLayouts(next);
    },
    [commitLayouts, publishLayouts],
  );

  return { groups, groupByRunId, groupByKey, layouts, setGroupLayout };
}

function useLiveRunRefresh(sheet: SheetMeta, liveRun: RunProgress | null, cache: RowCache): boolean {
  const runActive = liveRun?.sheetId === sheet.id && isActiveRunStatus(liveRun.status);

  // While a run streams results into this sheet, re-pull visible pages via the
  // shared poll loop (which also pauses in hidden tabs). The effect below covers
  // the final refresh when the run — or streaming — stops.
  const liveActive = isStreamingRunStatus(liveRun?.status) && liveRun?.sheetId === sheet.id;
  usePoll(() => cache.refresh(), { intervalMs: 400, active: runActive && liveActive });
  useEffect(() => {
    if (!runActive) return undefined;
    return () => {
      cache.refresh();
    };
  }, [runActive, liveActive, cache.refresh]); // eslint-disable-line react-hooks/exhaustive-deps

  // The scroll-to-new-columns behavior moved to the grid SURFACE: it needs
  // the header geometry (rightEdgeByName) + host width to smooth-scroll the
  // real scroll container with prefers-reduced-motion respected, none of
  // which live here.

  return runActive;
}

/** The run's freshly-created output columns, wrapped in the reusable
 *  ColumnAnnotation ("New columns" + a completed/total progress bar while
 *  populating). Cleared on click-dismiss or a short grace after the run
 *  reaches a terminal status. Detection uses an IDLE baseline
 *  (the column ids present the last moment no run was active) so a run's own
 *  new columns are exactly the AI columns pointed at it that the baseline never
 *  saw — the optimistic run is set BEFORE sheets refresh, so the baseline is
 *  frozen at pre-run columns and never absorbs them. */
function useRunColumnsAnnotation(
  sheet: SheetMeta,
  liveRun: RunProgress | null,
  runActive: boolean,
  hiddenColumnNames: string[],
): ColumnAnnotation | null {
  const liveRunId = liveRun?.runId ?? null;

  // Freeze the pre-run column ids at the moment a run BECOMES active (the
  // sanctioned adjust-state-during-render pattern — no ref read in render, no
  // setState-in-effect). The optimistic run is set BEFORE sheets refresh, so at
  // the activation render sheet.columns is still pre-run and the baseline never
  // absorbs the run's own new columns.
  const [baseline, setBaseline] = useState<{ runId: string | null; ids: Set<string> }>({
    runId: null,
    ids: new Set(),
  });
  const activeRunId = runActive ? liveRunId : null;
  if (activeRunId && baseline.runId !== activeRunId) {
    setBaseline({ runId: activeRunId, ids: new Set(sheet.columns.map((c) => c.id)) });
  }

  const baselineIds = baseline.runId === activeRunId ? baseline.ids : undefined;
  const newColumnIds = useMemo(() => {
    if (!runActive || !liveRunId || !baselineIds) return [] as string[];
    return sheet.columns.flatMap((c) => (
      !baselineIds.has(c.id) && c.latestRunId === liveRunId ? [c.id] : []
    ));
  }, [runActive, liveRunId, sheet.columns, baselineIds]);

  const hidden = useMemo(() => new Set(hiddenColumnNames), [hiddenColumnNames]);
  const visibleNewColumnIds = useMemo(
    () => newColumnIds.filter((id) => {
      const column = sheet.columns.find((candidate) => candidate.id === id);
      return column !== undefined && !hidden.has(column.name);
    }),
    [hidden, newColumnIds, sheet.columns],
  );
  const hiddenCount = newColumnIds.length - visibleNewColumnIds.length;

  const newColumnKey = newColumnIds.join(',');

  // Dismiss + terminal-grace are keyed by run id so a fresh run always shows a
  // fresh annotation even after the prior one was dismissed (a stale
  // dismissed/graced id simply never matches the new run's anchor id).
  const [dismissedRunId, setDismissedRunId] = useState<string | null>(null);
  const [gracedRunId, setGracedRunId] = useState<string | null>(null);

  // On terminal, hold the annotation for a short grace, then clear it. setState
  // fires only from the timeout callback — never synchronously in the effect
  // body — so a live run is never re-cleared.
  useEffect(() => {
    if (runActive || !liveRunId || !newColumnKey) return undefined;
    const timer = window.setTimeout(() => setGracedRunId(liveRunId), NEW_COLUMNS_GRACE_MS);
    return () => window.clearTimeout(timer);
  }, [runActive, liveRunId, newColumnKey]);

  return useMemo<ColumnAnnotation | null>(() => {
    if (!newColumnIds.length || !liveRunId) return null;
    if (dismissedRunId === liveRunId || gracedRunId === liveRunId) return null;
    const progress =
      runActive && liveRun ? { completed: liveRun.completedRows, total: liveRun.totalRows } : null;
    return {
      key: `run-new-columns-${liveRunId}`,
      columnIds: visibleNewColumnIds,
      message: `${newColumnIds.length} new column${newColumnIds.length === 1 ? '' : 's'} added${
        hiddenCount ? ` (${hiddenCount} hidden)` : ''
      }`,
      progress,
      onDismiss: () => setDismissedRunId(liveRunId),
      tone: runActive ? 'info' : 'success',
    };
  }, [newColumnIds, visibleNewColumnIds, hiddenCount, liveRunId, runActive, liveRun, dismissedRunId, gracedRunId]);
}

function selectedRowsPayload(sheetId: string, cache: RowCache, rows: CompactSelection) {
  const rowIndexes = rows.toArray();
  const rowIds: string[] = [];
  for (const rowIndex of rowIndexes) {
    const rowId = cache.getRow(rowIndex)?.id;
    if (rowId !== undefined) rowIds.push(rowId);
  }
  return { sheetId, rowIds, rowIndexes };
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName.toLowerCase();
  return (
    tag === 'input' ||
    tag === 'textarea' ||
    tag === 'select' ||
    target.isContentEditable ||
    target.closest('[role="textbox"]') !== null
  );
}

function useGridHeaderHandlers(
  columns: GridColumn[],
  gridColumns: ColumnDef[],
  columnGroups: NativeColumnGroupsState,
  onColumnOpen: (col: ColumnDef) => void,
  onColumnHeaderMenu: ((request: { column: ColumnDef; columnIndex: number; bounds: Rectangle }) => void) | undefined,
  onHeaderSort: ((col: ColumnDef) => void) | undefined,
  aiColumnTheme: Partial<Theme>,
) {
  const [groupMenu, setGroupMenu] = useState<GroupMenuState | null>(null);

  const closeGroupMenu = useCallback(() => setGroupMenu(null), []);

  const onHeaderClicked = useCallback(
    (colIdx: number) => {
      closeGroupMenu();
      const col = gridColumns[colIdx];
      if (!col) return;
      if (onHeaderSort) onHeaderSort(col);
      else onColumnOpen(col);
    },
    [closeGroupMenu, gridColumns, onColumnOpen, onHeaderSort],
  );

  const onHeaderMenuClick = useCallback(
    (colIdx: number, bounds: Rectangle) => {
      closeGroupMenu();
      const col = gridColumns[colIdx];
      if (col) onColumnHeaderMenu?.({ column: col, columnIndex: colIdx, bounds });
    },
    [closeGroupMenu, gridColumns, onColumnHeaderMenu],
  );

  const openGroupMenu = useCallback(
    (group: ColumnGroupModel, event: { bounds: Rectangle; preventDefault?: () => void }) => {
      event.preventDefault?.();
      setGroupMenu((current) => (
        current?.runId === group.runId ? null : { runId: group.runId, bounds: event.bounds }
      ));
    },
    [],
  );

  const onGroupHeaderClicked = useCallback(
    (colIdx: number, event: GroupHeaderClickedEventArgs) => {
      const groupKey = columns[colIdx]?.group;
      const group = groupKey ? columnGroups.groupByKey.get(groupKey) : undefined;
      if (group) openGroupMenu(group, event);
    },
    [columnGroups.groupByKey, columns, openGroupMenu],
  );

  const onGroupHeaderContextMenu = useCallback(
    (colIdx: number, event: GroupHeaderClickedEventArgs) => {
      const groupKey = columns[colIdx]?.group;
      const group = groupKey ? columnGroups.groupByKey.get(groupKey) : undefined;
      if (group) openGroupMenu(group, event);
    },
    [columnGroups.groupByKey, columns, openGroupMenu],
  );

  const onGroupHeaderRenamed = useCallback(
    (groupName: string, newValue: string) => {
      const next = newValue.trim();
      if (!next) return;
      const group = columnGroups.groups.find(
        (candidate) =>
          resolveColumnGroupLayout(candidate, columnGroups.layouts).label === groupName ||
          candidate.groupKey === groupName,
      );
      if (group) columnGroups.setGroupLayout(group.runId, { label: next });
    },
    [columnGroups],
  );

  const getGroupDetails = useCallback(
    (groupName: string) => {
      const group = columnGroups.groupByKey.get(groupName);
      if (!group) return { name: groupName };
      const layout = resolveColumnGroupLayout(group, columnGroups.layouts);
      return {
        name: layout.label,
        icon: 'aiBolt',
        overrideTheme: aiColumnTheme,
        actions: [
          {
            title: 'Group options',
            icon: GridColumnIcon.HeaderCode,
            onClick: (event: GridMouseGroupHeaderEventArgs) => openGroupMenu(group, event),
          },
        ],
      };
    },
    [columnGroups.groupByKey, columnGroups.layouts, openGroupMenu, aiColumnTheme],
  );

  const activeGroupMenu = groupMenu ? columnGroups.groupByRunId.get(groupMenu.runId) : undefined;
  const activeGroupLayout = activeGroupMenu
    ? resolveColumnGroupLayout(activeGroupMenu, columnGroups.layouts)
    : null;
  const activeGroupMenuBounds = activeGroupMenu && activeGroupLayout ? groupMenu?.bounds : null;

  return {
    activeGroupLayout,
    activeGroupMenu,
    activeGroupMenuBounds,
    getGroupDetails,
    onGroupHeaderClicked,
    onGroupHeaderContextMenu,
    onGroupHeaderRenamed,
    onHeaderClicked,
    onHeaderMenuClick,
  };
}

function NativeColumnGroupMenu({
  group,
  layout,
  bounds,
  onLayoutChange,
}: {
  group: ColumnGroupModel;
  layout: Required<ColumnGroupLayoutState>;
  bounds: Rectangle;
  onLayoutChange: NativeColumnGroupsState['setGroupLayout'];
}) {
  return (
    <div
      className="grid-header-menu"
      data-testid="native-column-group-menu"
      style={{
        left: Math.min(bounds.x, window.innerWidth - 266),
        top: Math.min(bounds.y + bounds.height + 4, window.innerHeight - 190),
      }}
    >
      <div className="grid-header-menu-title">
        <span>{layout.label}</span>
        <span>AI group</span>
      </div>
      <label className="grid-header-menu-item">
        <input
          type="checkbox"
          data-testid="native-column-group-show-confidence"
          checked={layout.showConfidence}
          onChange={(event) =>
            onLayoutChange(group.runId, {
              showConfidence: event.target.checked,
            })
          }
        />
        Confidence columns
      </label>
      <label className="grid-header-menu-item">
        <input
          type="checkbox"
          data-testid="native-column-group-show-justification"
          checked={layout.showJustification}
          onChange={(event) =>
            onLayoutChange(group.runId, {
              showJustification: event.target.checked,
            })
          }
        />
        Justification columns
      </label>
    </div>
  );
}

interface SheetGridController {
  sheetId: string;
  columnGroups: NativeColumnGroupsState;
  columns: GridColumn[];
  frozenColumnCount: number;
  getCellContent(item: Item): GridCell;
  gridColumns: ColumnDef[];
  gridReady: boolean;
  gridRef: MutableRefObject<DataEditorRef | null>;
  gridSelection: GridSelection;
  gridTheme: Partial<Theme>;
  hasNativeColumnGroups: boolean;
  headerHandlers: ReturnType<typeof useGridHeaderHandlers>;
  hostRef: MutableRefObject<HTMLDivElement | null>;
  hiddenBoundaries: HiddenColumnBoundary[];
  columnsEndX: number;
  rightEdgeByName: Map<string, number>;
  columnAnnotations: ColumnAnnotation[];
  onRevealColumns?(columns: string[]): void;
  onAddColumnAtEnd?(anchor: { x: number; y: number }): void;
  onCellActivated(item: Item): void;
  onCellClicked(item: Item, event: CellClickedEventArgs): void;
  onCellEdited(item: Item, newValue: EditableGridCell): void;
  onColumnMoved(startIndex: number, endIndex: number): void;
  onColumnResize: ColumnModel['onColumnResize'];
  onColumnResizeEnd(column: GridColumn, newSize: number, columnIndex: number): void;
  onGridSelectionChange(selection: GridSelection): void;
  onVisibleRegionChanged(range: Rectangle): void;
  rowCount: number;
  rowHeight: number;
  detailIconBounds: Rectangle | null;
  openSelectedCell(): void;
}

function useSheetGridController({
  sheet, dataVersion, liveRun, rowHeight, wrapText, childSheet, parentRowFilter,
  activeFilter, activeSort, columnOrder, lensRowIds, lensScores,
  previewColumns, previewCells,
  rowDrawerOpen = false, frozenColumnCount = 1, columnGroupsStorageScope, columnGroupsVersion,
  hiddenColumnNames = EMPTY_COLUMN_NAMES, userHiddenColumnNames = EMPTY_COLUMN_NAMES,
  onRevealColumns, onAddColumnAtEnd,
  onRowOpen, onColumnOpen, onHeaderSort, onSelectedRowsChange,
  onColumnHeaderMenu, onColumnGroupsChange, onColumnOrderChange, onCellEdit,
  onFacetValueFilter, rowCacheStore,
  columnAnnotations: externalColumnAnnotations = EMPTY_COLUMN_ANNOTATIONS,
}: SheetGridProps): SheetGridController {
  const {
    projectApi,
    audioPlayback,
    chromePreferences: { projectId },
  } = useWorkspaceStores();
  const gridAudioRunActive =
    liveRun?.sheetId === sheet.id && isActiveRunStatus(liveRun.status);
  const audioPlaybackState = useSyncExternalStore(
    audioPlayback.store.subscribe,
    audioPlayback.store.get,
    audioPlayback.store.get,
  );
  const lensActive = lensRowIds != null;
  const baseRowCount = lensActive
    ? lensRowIds.length
    : parentRowFilter
      ? parentRowFilter.count
      : sheet.rowCount;
  const lensRowIdsKey = lensActive ? lensRowIds.join(',') : '';
  const dataOptions = useMemo<SheetDataOptions>(
    () =>
      resolveRowCacheScope({
        parentRowId: parentRowFilter?.parentRowId,
        filter: activeFilter,
        sort: activeSort,
        lensRowIds: lensActive ? (lensRowIdsKey ? lensRowIdsKey.split(',').map(Number) : []) : null,
      }),
    [activeFilter, activeSort, parentRowFilter?.parentRowId, lensActive, lensRowIdsKey],
  );
  const fallbackRowCacheStore = useMemo(() => createRowCacheStore(), []);
  const cache = useRowCache(
    sheet.id,
    baseRowCount,
    dataVersion,
    rowCacheStore ?? fallbackRowCacheStore,
    dataOptions,
    projectApi,
  );
  const rowCount = cache.rowCount;
  const gridRef = useRef<DataEditorRef | null>(null);
  const visibleRegion = useRef<Rectangle>({ x: 0, y: 0, width: 1, height: 1 });
  const selectedItem = useRef<Item | null>(null);
  const lastOpenedItem = useRef<{ item: Item; at: number } | null>(null);
  const lastEntityMentionToggle = useRef<{ key: string; at: number } | null>(null);
  const [gridSelection, setGridSelection] = useState<GridSelection>(() => emptyGridSelection());
  // This belongs to the grid view rather than the row model: revealing a
  // mention list is a transient reading aid, never a data mutation.
  const [expandedEntityMentionCells, setExpandedEntityMentionCells] = useState<Set<string>>(
    () => new Set(),
  );
  const [detailIconBounds, setDetailIconBounds] = useState<Rectangle | null>(null);
  useEffect(() => {
    if (!import.meta.env.DEV) return;
    (
      window as unknown as {
        __frisketGridSelection?: { cell: readonly [number, number] | null; rows: number[] };
      }
    ).__frisketGridSelection = {
      cell: gridSelection.current?.cell ?? null,
      rows: gridSelection.rows.toArray(),
    };
  }, [gridSelection]);
  const columnGroupChangeSink = useColumnGroupChangeSink(onColumnGroupsChange);
  const columnGroups = useNativeColumnGroups(
    sheet,
    columnGroupsStorageScope,
    columnGroupsVersion,
    columnGroupChangeSink,
    projectApi,
  );
  const previewNewColumns = useMemo(
    () => (previewColumns ?? []).filter((col) => col.overwritesColumnId == null),
    [previewColumns],
  );
  const {
    columns,
    gridColumns,
    gridTheme,
    aiColumnTheme,
    gridCellPalette,
    onColumnResize: onColumnResizeRaw,
    hiddenBoundaries,
    columnsEndX,
    rightEdgeByName,
  } =
    useColumnModel(
      sheet,
      columnGroupsStorageScope,
      columnOrder,
      hiddenColumnNames,
      userHiddenColumnNames,
      childSheet,
      columnGroups,
      lensActive,
      previewNewColumns,
      activeSort,
    );
  const runActive = useLiveRunRefresh(sheet, liveRun, cache);
  const hasNativeColumnGroups = columnGroups.groups.length > 0;
  const gridReady = rowCount === 0 || cache.getRow(0) !== undefined;
  const headerHandlers = useGridHeaderHandlers(
    columns,
    gridColumns,
    columnGroups,
    onColumnOpen,
    onColumnHeaderMenu,
    onHeaderSort,
    aiColumnTheme,
  );

  const previewColumnAt = useCallback((col: number) => {
    const previewBase = gridColumns.length + (childSheet ? 1 : 0) + (lensActive ? 2 : 0);
    if (col >= previewBase) return previewNewColumns[col - previewBase];
    const def = gridColumns[col];
    return def && previewColumns?.find((column) => column.overwritesColumnId === def.id);
  }, [gridColumns, childSheet, lensActive, previewNewColumns, previewColumns]);

  const previewAt = useCallback(([col, rowIdx]: Item): PreviewCellDetail | null => {
    const column = previewColumnAt(col);
    const row = cache.getRow(rowIdx);
    const cell = column && row ? previewCells?.[row.id]?.[column.name] : undefined;
    // Presence of a cell, not truthiness of its value, distinguishes sampling.
    return column && cell !== undefined ? { column, cell } : null;
  }, [cache, previewColumnAt, previewCells]);

  // Glide retains canvas selection across sheet props, so clear only its local selection here.
  useEffect(() => {
    let alive = true;
    selectedItem.current = null;
    lastOpenedItem.current = null;
    lastEntityMentionToggle.current = null;
    queueMicrotask(() => {
      if (!alive) return;
      setExpandedEntityMentionCells(new Set());
      setGridSelection(emptyGridSelection());
      setDetailIconBounds(null);
    });
    return () => {
      alive = false;
    };
  }, [sheet.id]);

  // Glide preserves scrollTop pixels across row-height changes, so rescale to keep the same top row.
  const hostRef = useRef<HTMLDivElement | null>(null);
  const prevRowHeight = useRef(rowHeight);
  useEffect(() => {
    if (prevRowHeight.current === rowHeight) return;
    const ratio = rowHeight / prevRowHeight.current;
    prevRowHeight.current = rowHeight;
    const scroller = hostRef.current?.querySelector<HTMLElement>('.dvn-scroller');
    if (scroller && scroller.scrollTop > 0) {
      scroller.scrollTop = Math.round(scroller.scrollTop * ratio);
    }
  }, [rowHeight]);

  const pendingColIds = useMemo<Set<string>>(() => {
    if (!runActive || !liveRun) return new Set();
    const ids = new Set<string>();
    for (const col of sheet.columns) {
      if (col.ai && col.latestRunId === liveRun.runId) {
        ids.add(col.id);
      }
    }
    return ids;
  }, [runActive, liveRun, sheet.columns]);

  const pendingRowIds = useMemo<Set<string> | null>(() => {
    if (!runActive || !liveRun) return null;
    const ids = liveRun.targetRowIds;
    return ids && ids.length ? new Set(ids) : null;
  }, [runActive, liveRun]);

  const runColumnsAnnotation = useRunColumnsAnnotation(
    sheet,
    liveRun,
    runActive,
    hiddenColumnNames,
  );
  const columnAnnotations = useMemo<ColumnAnnotation[]>(
    () =>
      runColumnsAnnotation
        ? [...externalColumnAnnotations, runColumnsAnnotation]
        : externalColumnAnnotations,
    [runColumnsAnnotation, externalColumnAnnotations],
  );

  useEffect(() => {
    if (!import.meta.env.DEV) return undefined;
    const w = window as unknown as { __frisketLiveFill?: unknown };
    w.__frisketLiveFill = {
      runId: runActive ? liveRun?.runId ?? null : null,
      status: liveRun?.status ?? null,
      sheetId: sheet.id,
      pendingColumnIds: [...pendingColIds],
      targetRowIds: pendingRowIds ? [...pendingRowIds] : null,
      rowCount: cache.rowCount,
      cellValue: (rowIndex: number, columnId: string) =>
        cache.getRow(rowIndex)?.cells[columnId] ?? null,
      cellError: (rowIndex: number, columnId: string) =>
        cache.getRow(rowIndex)?.cellErrors?.[columnId] ?? null,
    };
    return () => {
      delete w.__frisketLiveFill;
    };
  }, [runActive, liveRun, sheet.id, pendingColIds, pendingRowIds, cache]);

  const getCellContent = useCallback(
    (item: Item): GridCell => {
      const [col, rowIdx] = item;
      if (childSheet && col === gridColumns.length) {
        const row = cache.getRow(rowIdx);
        if (row === undefined) return { kind: GridCellKind.Loading, allowOverlay: false };
        const n = row.childCount ?? 0;
        return {
          kind: GridCellKind.Bubble,
          data: n > 0 ? [childChipLabel(n, childSheet.name)] : [],
          allowOverlay: false,
          ...(n > 0 ? { cursor: 'pointer' } : {}),
        };
      }
      const lensBase = gridColumns.length + (childSheet ? 1 : 0);
      if (lensActive && col >= lensBase && col < lensBase + 2) {
        const row = cache.getRow(rowIdx);
        if (row === undefined) return { kind: GridCellKind.Loading, allowOverlay: false };
        const metric = lensScores?.[row.id] ?? null;
        const value = col === lensBase ? metric?.distance ?? null : metric?.score ?? null;
        const text = value == null ? '—' : value.toFixed(3);
        return {
          kind: GridCellKind.Text,
          data: text,
          displayData: text,
          allowOverlay: false,
          readonly: true,
        };
      }
      const previewBase = lensBase + (lensActive ? 2 : 0);
      if (previewNewColumns.length && col >= previewBase) {
        const previewCol = previewNewColumns[col - previewBase];
        if (!previewCol) return { kind: GridCellKind.Loading, allowOverlay: false };
        const row = cache.getRow(rowIdx);
        if (row === undefined) return { kind: GridCellKind.Loading, allowOverlay: false };
        return previewGridCell(
          previewCol,
          previewCells?.[row.id]?.[previewCol.name],
          { wrap: wrapText, projectId, gridCellPalette },
        );
      }
      const def = gridColumns[col];
        // Glide may request the previous sheet's trailing index for one frame after a swap.
      if (!def) return { kind: GridCellKind.Loading, allowOverlay: false };
      const preview = previewAt(item);
      if (preview) {
        return previewGridCell(preview.column, preview.cell, { wrap: wrapText, projectId, gridCellPalette });
      }
      const row = cache.getRow(rowIdx);
      const pending =
        pendingColIds.has(def.id) &&
        (pendingRowIds === null || (row !== undefined && pendingRowIds.has(row.id)));
      const entityMentionCellKey = row ? JSON.stringify([row.id, def.id]) : null;
      const cell = withOneClickFacetCursor(
        buildCell(def, row, {
          projectId,
          wrap: wrapText,
          pending,
          editable: cellEditability(def),
          gridCellPalette,
          expandEntityMentions: Boolean(
            entityMentionCellKey && expandedEntityMentionCells.has(entityMentionCellKey),
          ),
        }),
        def,
        row,
      );
      const media = audioMedia(cell);
      if (!media || !row) return cell;
      const source = createAudioPlaybackSource({
        url: media.url,
        label: media.label,
        sheetId: sheet.id,
        rowId: row.id,
        columnId: def.id,
      });
      return withAudioPlayback(
        cell,
        audioPlaybackState.source?.key === source.key && audioPlaybackState.playing,
      );
    },
    [
      gridColumns,
      cache,
      wrapText,
      pendingColIds,
      pendingRowIds,
      childSheet,
      lensActive,
      lensScores,
      previewNewColumns,
      previewCells,
      previewAt,
      projectId,
      gridCellPalette,
      sheet.id,
      audioPlaybackState,
      expandedEntityMentionCells,
    ],
  );

  // Glide bounds are viewport-relative; convert them to host-relative overlay coordinates.
  const recomputeDetailIconBounds = useCallback(() => {
    const cell = selectedItem.current;
    const host = hostRef.current;
    if (!cell || !host) {
      setDetailIconBounds(null);
      return;
    }
    const bounds = gridRef.current?.getBounds(cell[0], cell[1]);
    if (!bounds) {
      setDetailIconBounds(null);
      return;
    }
    const hostRect = host.getBoundingClientRect();
    const next: Rectangle = {
      x: bounds.x - hostRect.left,
      y: bounds.y - hostRect.top,
      width: bounds.width,
      height: bounds.height,
    };
    setDetailIconBounds((prev) =>
      prev &&
      prev.x === next.x &&
      prev.y === next.y &&
      prev.width === next.width &&
      prev.height === next.height
        ? prev
        : next,
    );
  }, []);

  const pendingCellReveal = useSyncExternalStore(
    subscribeGridCellReveal,
    getPendingGridCellReveal,
    getPendingGridCellReveal,
  );
  const loadRevealedRows = cache.onVisibleRowsChanged;
  useEffect(() => {
    if (
      !pendingCellReveal ||
      pendingCellReveal.projectId !== projectId ||
      pendingCellReveal.sheetId !== sheet.id ||
      !gridReady
    ) {
      return;
    }

    const columnIndex = gridColumns.findIndex(
      (column) =>
        column.id === pendingCellReveal.columnId ||
        column.name === pendingCellReveal.columnName,
    );
    if (columnIndex < 0) {
      const hiddenColumn = sheet.columns.find(
        (column) =>
          column.id === pendingCellReveal.columnId ||
          column.name === pendingCellReveal.columnName,
      );
      if (hiddenColumn && onRevealColumns) {
        onRevealColumns([hiddenColumn.name]);
        return;
      }
      consumeGridCellReveal(pendingCellReveal.requestId);
      return;
    }

    let cancelled = false;
    void projectApi
      .locateSheetRow(sheet.id, pendingCellReveal.rowId, dataOptions)
      .then((location) => {
        if (cancelled || getPendingGridCellReveal()?.requestId !== pendingCellReveal.requestId) {
          return;
        }
        if (!location.found || location.index == null) {
          consumeGridCellReveal(pendingCellReveal.requestId);
          return;
        }

        const rowIndex = location.index;
        const cell: Item = [columnIndex, rowIndex];
        loadRevealedRows(rowIndex, rowIndex);
        selectedItem.current = cell;
        setGridSelection({
          columns: CompactSelection.empty(),
          rows: CompactSelection.empty(),
          current: {
            cell,
            range: { x: columnIndex, y: rowIndex, width: 1, height: 1 },
            rangeStack: [],
          },
        });
        consumeGridCellReveal(pendingCellReveal.requestId);
        requestAnimationFrame(() => {
          gridRef.current?.scrollTo(columnIndex, rowIndex, 'both', 24, 24, {
            hAlign: 'center',
            vAlign: 'center',
          });
          gridRef.current?.focus();
          recomputeDetailIconBounds();
        });
      })
      .catch(() => {
        if (!cancelled) consumeGridCellReveal(pendingCellReveal.requestId);
      });

    return () => {
      cancelled = true;
    };
  }, [
    dataOptions,
    gridColumns,
    gridReady,
    loadRevealedRows,
    onRevealColumns,
    pendingCellReveal,
    projectApi,
    projectId,
    recomputeDetailIconBounds,
    sheet.columns,
    sheet.id,
  ]);

  const onColumnResize = useCallback(
    (column: GridColumn, newSize: number) => {
      onColumnResizeRaw(column, newSize);
      recomputeDetailIconBounds();
    },
    [onColumnResizeRaw, recomputeDetailIconBounds],
  );

  const onColumnResizeEnd = useCallback(
    (_column: GridColumn, _newSize: number, columnIndex: number) => {
      // Glide's resize fast path can finish by blitting its temporary
      // single-line raster. Explicitly damaging the visible cells makes it
      // rebuild them with their existing allowWrapping flag at the new width.
      recomputeDetailIconBounds();
      requestAnimationFrame(() => {
        const range = visibleRegion.current;
        const firstRow = Math.max(0, Math.floor(range.y));
        const lastRow = Math.min(rowCount, Math.ceil(range.y + range.height));
        if (lastRow <= firstRow) return;
        gridRef.current?.updateCells(
          Array.from({ length: lastRow - firstRow }, (_, offset) => ({
            cell: [columnIndex, firstRow + offset] as Item,
          })),
        );
      });
    },
    [gridRef, recomputeDetailIconBounds, rowCount],
  );

  useEffect(() => {
    // Glide can retain its previous single-line raster when wrapping changes.
    // Repaint only the visible cells so the new allowWrapping values take
    // effect without remounting the grid or losing scroll and selection.
    requestAnimationFrame(() => {
      const range = visibleRegion.current;
      const firstColumn = Math.max(0, Math.floor(range.x));
      const lastColumn = Math.min(gridColumns.length, Math.ceil(range.x + range.width));
      const firstRow = Math.max(0, Math.floor(range.y));
      const lastRow = Math.min(rowCount, Math.ceil(range.y + range.height));
      const cells: { cell: Item }[] = [];
      for (let column = firstColumn; column < lastColumn; column += 1) {
        for (let row = firstRow; row < lastRow; row += 1) cells.push({ cell: [column, row] });
      }
      if (cells.length) gridRef.current?.updateCells(cells);
    });
  }, [gridColumns.length, gridRef, rowCount, wrapText]);

  useEffect(() => {
    window.addEventListener('resize', recomputeDetailIconBounds);
    return () => window.removeEventListener('resize', recomputeDetailIconBounds);
  }, [recomputeDetailIconBounds]);

  const onVisibleRegionChanged = useCallback(
    (range: Rectangle) => {
      visibleRegion.current = range;
      cache.onVisibleRowsChanged(range.y, range.y + range.height);
      recomputeDetailIconBounds();
    },
    [cache, recomputeDetailIconBounds],
  );

  const openCell = useCallback(
    (item: Item) => {
      const [col, rowIdx] = item;
      if (col < 0) return;
      const row = cache.getRow(rowIdx);
      if (!row) return;
      const prev = lastOpenedItem.current;
      const now = performance.now();
      if (prev && prev.item[0] === col && prev.item[1] === rowIdx && now - prev.at < 100) {
        return;
      }
      lastOpenedItem.current = { item, at: now };
      if (childSheet && col === gridColumns.length) {
        if ((row.childCount ?? 0) > 0) {
          window.dispatchEvent(
            new CustomEvent('frisket:reveal-children', {
              detail: {
                parentSheetId: sheet.id,
                parentRowId: row.id,
                parentRowIndex: row.index,
                childCount: row.childCount,
              },
            }),
          );
        }
        return;
      }
      selectedItem.current = [col, rowIdx];
      onRowOpen(row, gridColumns[col], previewAt(item));
    },
    [
      cache,
      onRowOpen,
      childSheet,
      sheet.id,
      gridColumns,
      previewAt,
    ],
  );

  const toggleGridAudio = useCallback(
    (item: Item, displayedCell = getCellContent(item)): boolean => {
      const [col, rowIndex] = item;
      const media = audioMedia(displayedCell);
      const row = cache.getRow(rowIndex);
      const column = gridColumns[col];
      if (!media || !row || !column) return false;
      audioPlayback.toggle(createAudioPlaybackSource({
        url: media.url,
        label: media.label,
        sheetId: sheet.id,
        rowId: row.id,
        columnId: column.id,
      }), {
        // A changing grid cannot start (or restart) audio, but the active
        // source must remain pausable from the same control.
        allowStart: !gridAudioRunActive,
      });
      return true;
    },
    [audioPlayback, cache, getCellContent, gridAudioRunActive, gridColumns, sheet.id],
  );

  const toggleEntityMentionExpansion = useCallback(
    (item: Item, displayedCell = getCellContent(item)): boolean => {
      const [col, rowIndex] = item;
      const row = cache.getRow(rowIndex);
      const column = gridColumns[col];
      if (!row || !column || !isExpandableEntityMentionCell(column, displayedCell)) return false;
      const key = JSON.stringify([row.id, column.id]);
      const now = performance.now();
      // Glide can report the two clicks that make up a double-click before it
      // reports activation. Treat that as one reveal, rather than opening and
      // immediately reclosing the same cell.
      if (
        lastEntityMentionToggle.current?.key === key
        && now - lastEntityMentionToggle.current.at < 250
      ) {
        return true;
      }
      lastEntityMentionToggle.current = { key, at: now };
      setExpandedEntityMentionCells((previous) => {
        const next = new Set(previous);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      });
      return true;
    },
    [cache, getCellContent, gridColumns],
  );

  const onCellClicked = useCallback(
    (item: Item, event: CellClickedEventArgs) => {
      const [col, rowIdx] = item;
      if (col < 0) return;
      const row = cache.getRow(rowIdx);
      if (!row) return;
      if (previewAt(item)) {
        event.preventDefault();
        openCell(item);
        return;
      }
      const displayedCell = getCellContent(item);
      const media = audioMedia(displayedCell);
      if (
        media
        && isAudioPlayButtonHit(event.localEventX, event.localEventY, event.bounds.height)
        && toggleGridAudio(item, displayedCell)
      ) {
        event.preventDefault();
        return;
      }
      if (childSheet && col === gridColumns.length) {
        openCell(item);
        return;
      }
      if (toggleEntityMentionExpansion(item, displayedCell)) return;
      const column = gridColumns[col];
      if (column && onFacetValueFilter) {
        const facet = oneClickFacetValue(column, row);
        if (facet) {
          onFacetValueFilter(column, facet.value, facet.operator);
          return;
        }
      }
      if (rowDrawerOpen) {
        openCell(item);
        return;
      }
      selectedItem.current = item;
    },
    [
      cache,
      childSheet,
      gridColumns,
      onFacetValueFilter,
      openCell,
      rowDrawerOpen,
      getCellContent,
      toggleGridAudio,
      toggleEntityMentionExpansion,
      previewAt,
    ],
  );

  const onCellActivated = useCallback(
    (item: Item) => {
      if (previewAt(item)) {
        openCell(item);
        return;
      }
      const displayedCell = getCellContent(item);
      if (toggleGridAudio(item, displayedCell)) {
        return;
      }
      if (toggleEntityMentionExpansion(item, displayedCell)) return;
      if (isOverlayEditable(displayedCell)) return;
      openCell(item);
    },
    [openCell, getCellContent, toggleGridAudio, toggleEntityMentionExpansion, previewAt],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return;
      if (event.key !== 'Enter' && event.key !== ' ') return;
      if (isTypingTarget(event.target)) return;
      const item = selectedItem.current;
      if (!item) return;
      if (previewAt(item)) {
        event.preventDefault();
        openCell(item);
        return;
      }
      const displayedCell = getCellContent(item);
      if (toggleEntityMentionExpansion(item, displayedCell)) {
        event.preventDefault();
        return;
      }
      const overlayEditable = event.key === 'Enter' && isOverlayEditable(displayedCell);
      if (!shouldOpenDrawerOnKey(event.key, overlayEditable)) return;
      event.preventDefault();
      openCell(item);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [openCell, getCellContent, toggleEntityMentionExpansion, previewAt]);

  const openSelectedCell = useCallback(() => {
    const item = selectedItem.current;
    if (item) openCell(item);
  }, [openCell]);

  const onGridSelectionChange = useCallback(
    (selection: GridSelection) => {
      setGridSelection(selection);
      onSelectedRowsChange?.(selectedRowsPayload(sheet.id, cache, selection.rows));
      const cell = selection.current?.cell;
      if (!cell) {
        selectedItem.current = null;
        recomputeDetailIconBounds();
        return;
      }
      const selected = selectedItem.current;
      if (selected && selected[0] === cell[0] && selected[1] === cell[1]) return;
      selectedItem.current = cell;
      recomputeDetailIconBounds();
      if (rowDrawerOpen && !(childSheet && cell[0] === gridColumns.length)) openCell(cell);
    },
    [
      cache,
      childSheet,
      gridColumns.length,
      onSelectedRowsChange,
      openCell,
      recomputeDetailIconBounds,
      rowDrawerOpen,
      sheet.id,
    ],
  );

  const onColumnMoved = useCallback(
    (startIndex: number, endIndex: number) => {
      if (!onColumnOrderChange) return;
      const next = [...gridColumns];
      const [moved] = next.splice(startIndex, 1);
      if (!moved) return;
      next.splice(endIndex, 0, moved);
      onColumnOrderChange(next.map((col) => col.name));
    },
    [gridColumns, onColumnOrderChange],
  );

  const onCellEdited = useCallback(
    (item: Item, newValue: EditableGridCell) => {
      if (previewAt(item)) return;
      const [col, rowIdx] = item;
      const def = gridColumns[col];
      if (!def || def.ai) return;
      const row = cache.getRow(rowIdx);
      const value = editableValue(newValue);
      if (!row || value === undefined) return;
      void onCellEdit(row, def, value).then(
        () => cache.refresh(),
        () => cache.refresh(),
      );
    },
    [cache, onCellEdit, gridColumns, previewAt],
  );

  return useMemo(
    () => ({
      sheetId: sheet.id,
      columnGroups,
      columns,
      frozenColumnCount,
      getCellContent,
      gridColumns,
      gridReady,
      gridRef,
      gridSelection,
      gridTheme,
      hasNativeColumnGroups,
      headerHandlers,
      hostRef,
      hiddenBoundaries,
      columnsEndX,
      rightEdgeByName,
      columnAnnotations,
      onRevealColumns,
      onAddColumnAtEnd,
      onCellActivated,
      onCellClicked,
      onCellEdited,
      onColumnMoved,
      onColumnResize,
      onColumnResizeEnd,
      onGridSelectionChange,
      onVisibleRegionChanged,
      rowCount,
      rowHeight,
      detailIconBounds,
      openSelectedCell,
    }),
    [
      sheet.id,
      columnGroups,
      columns,
      frozenColumnCount,
      getCellContent,
      gridColumns,
      gridReady,
      gridRef,
      gridSelection,
      gridTheme,
      hasNativeColumnGroups,
      headerHandlers,
      hostRef,
      hiddenBoundaries,
      columnsEndX,
      rightEdgeByName,
      columnAnnotations,
      onRevealColumns,
      onAddColumnAtEnd,
      onCellActivated,
      onCellClicked,
      onCellEdited,
      onColumnMoved,
      onColumnResize,
      onColumnResizeEnd,
      onGridSelectionChange,
      onVisibleRegionChanged,
      rowCount,
      rowHeight,
      detailIconBounds,
      openSelectedCell,
    ],
  );
}

function SheetGridSurface({ controller }: { controller: SheetGridController }) {
  const {
    sheetId,
    columnGroups,
    columns,
    frozenColumnCount,
    getCellContent,
    gridColumns,
    gridReady,
    gridRef,
    gridSelection,
    gridTheme,
    hasNativeColumnGroups,
    headerHandlers,
    hostRef,
    hiddenBoundaries,
    columnsEndX,
    rightEdgeByName,
    columnAnnotations,
    onRevealColumns,
    onAddColumnAtEnd,
    onCellActivated,
    onCellClicked,
    onCellEdited,
    onColumnMoved,
    onColumnResize,
    onColumnResizeEnd,
    onGridSelectionChange,
    onVisibleRegionChanged,
    rowCount,
    rowHeight,
    detailIconBounds,
    openSelectedCell,
  } = controller;

  const [scrollLeft, setScrollLeft] = useState(0);
  const [hostWidth, setHostWidth] = useState(0);
  useEffect(() => {
    const host = hostRef.current;
    const scroller = host?.querySelector<HTMLElement>('.dvn-scroller');
    if (!host || !scroller) return;
    const sync = () => {
      setScrollLeft(scroller.scrollLeft);
      setHostWidth(host.clientWidth);
    };
    sync();
    scroller.addEventListener('scroll', sync, { passive: true });
    const observer = new ResizeObserver(sync);
    observer.observe(host);
    return () => {
      scroller.removeEventListener('scroll', sync);
      observer.disconnect();
    };
  }, [hostRef, gridReady, columns.length]);

  const headerTop = hasNativeColumnGroups ? GROUP_HEADER_HEIGHT : 0;
  const frozenWidth = useMemo(() => {
    let w = ROW_MARKER_WIDTH;
    for (let i = 0; i < Math.min(frozenColumnCount, columns.length); i += 1) {
      const col = columns[i];
      w += (col && 'width' in col ? col.width : undefined) ?? 140;
    }
    return w;
  }, [columns, frozenColumnCount]);
  const bandX = (x: number): number =>
    x <= frozenWidth ? x : Math.max(frozenWidth, x - scrollLeft);

  const addColumnOverscrollX = addColumnScrollGutter(Boolean(onAddColumnAtEnd), hostWidth, columnsEndX);

  useEffect(() => {
    const revealColumn = (event: Event) => {
      const columnName = (event as CustomEvent<{ columnName?: unknown }>).detail?.columnName;
      if (typeof columnName !== 'string') return;
      const index = gridColumns.findIndex((column) => column.name === columnName);
      if (index < 0) return;
      gridRef.current?.scrollTo(index, 0, 'horizontal', 24, 0, { hAlign: 'center' });
    };
    window.addEventListener('frisket:reveal-grid-column', revealColumn);
    return () => window.removeEventListener('frisket:reveal-grid-column', revealColumn);
  }, [gridColumns, gridRef]);

  const walkthroughColumnAnchors = useMemo(() => {
    return gridColumns.flatMap((column, index) => {
      const leftRaw = index === 0
        ? ROW_MARKER_WIDTH
        : rightEdgeByName.get(gridColumns[index - 1].name);
      const rightRaw = rightEdgeByName.get(column.name);
      return leftRaw === undefined || rightRaw === undefined
        ? []
        : [{ column, leftRaw, rightRaw }];
    });
  }, [gridColumns, rightEdgeByName]);

  const annotationPlacements = useMemo(() => {
    const placementColumns = gridColumns.map((column, index) => ({
      id: column.id,
      group: columns[index]?.group,
    }));
    return columnAnnotations.flatMap((annotation) => {
      const placement = resolveColumnAnnotationPlacement(
        annotation.columnIds,
        placementColumns,
        GROUP_HEADER_HEIGHT,
      );
      if (!placement) return [];
      const { first, last, headerOffset } = placement;
      const leftRaw =
        first === 0 ? ROW_MARKER_WIDTH : rightEdgeByName.get(gridColumns[first - 1].name);
      const rightRaw = rightEdgeByName.get(gridColumns[last].name);
      if (leftRaw === undefined || rightRaw === undefined) return [];
      return [{ annotation, leftRaw, rightRaw, headerOffset }];
    });
  }, [columnAnnotations, columns, gridColumns, rightEdgeByName]);

  // Drive Glide's scroller directly and honor prefers-reduced-motion.
  const lastScrolledKeyRef = useRef<string | null>(null);
  useEffect(() => {
    lastScrolledKeyRef.current = null;
  }, [sheetId]);
  useEffect(() => {
    if (!gridReady) return;
    const target = annotationPlacements[annotationPlacements.length - 1];
    if (!target || lastScrolledKeyRef.current === target.annotation.key) return;
    const host = hostRef.current;
    const scroller = host?.querySelector<HTMLElement>('.dvn-scroller');
    if (!host || !scroller) return;
    lastScrolledKeyRef.current = target.annotation.key;
    const desired = Math.max(0, target.rightRaw - host.clientWidth + 24);
    if (Math.abs(desired - scroller.scrollLeft) < 2) return;
    const reduce =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    scroller.scrollTo({ left: desired, behavior: reduce ? 'auto' : 'smooth' });
  }, [annotationPlacements, gridReady, hostRef]);

  return (
    <div
      className="grid-host"
      data-testid={gridReady ? 'grid' : 'grid-loading'}
      tabIndex={-1}
      data-native-column-groups={hasNativeColumnGroups ? 'true' : 'false'}
      data-group-header-height={hasNativeColumnGroups ? String(GROUP_HEADER_HEIGHT) : '0'}
      data-visible-column-names={gridColumns.map((column) => column.name).join(',')}
      data-frozen-columns={String(Math.max(0, Math.min(frozenColumnCount, gridColumns.length)))}
      ref={hostRef}
    >
        {/* Do not key on theme colors; remounting Glide loses its internal scroll position. */}
      <DataEditor
        ref={gridRef}
        width="100%"
        height="100%"
        columns={columns}
        rows={rowCount}
        getCellContent={getCellContent}
        customRenderers={customRenderers}
        headerIcons={headerIcons}
        theme={gridTheme}
        rowMarkers={{ kind: 'both', width: 40 }}
        rowSelectionMode="multi"
        rowHeight={rowHeight}
        headerHeight={34}
        groupHeaderHeight={GROUP_HEADER_HEIGHT}
        smoothScrollX
        smoothScrollY
        overscrollX={addColumnOverscrollX}
        freezeColumns={Math.max(0, Math.min(frozenColumnCount, columns.length))}
        verticalBorder={false}
        getCellsForSelection
        gridSelection={gridSelection}
        onGridSelectionChange={onGridSelectionChange}
        onColumnResize={onColumnResize}
        onColumnResizeEnd={onColumnResizeEnd}
        onVisibleRegionChanged={onVisibleRegionChanged}
        onCellClicked={onCellClicked}
        onCellActivated={onCellActivated}
        onCellEdited={onCellEdited}
        onHeaderClicked={headerHandlers.onHeaderClicked}
        onHeaderMenuClick={headerHandlers.onHeaderMenuClick}
        getGroupDetails={headerHandlers.getGroupDetails}
        onGroupHeaderClicked={headerHandlers.onGroupHeaderClicked}
        onGroupHeaderContextMenu={headerHandlers.onGroupHeaderContextMenu}
        onGroupHeaderRenamed={headerHandlers.onGroupHeaderRenamed}
        onColumnMoved={onColumnMoved}
      />
      {!gridReady && (
        <PanelLoading
          className="main-view-loading grid-view-loading"
          testId="grid-data-loading"
          label="Loading grid…"
        />
      )}
      {gridReady && walkthroughColumnAnchors.map(({ column, leftRaw, rightRaw }) => {
        const left = bandX(leftRaw);
        const right = bandX(rightRaw);
        return (
          <span
            key={column.id}
            className="grid-walkthrough-column-anchor"
            data-testid={`grid-column-${column.name}`}
            data-walkthrough-grid-column={column.name}
            aria-hidden="true"
            style={{
              top: headerTop,
              left,
              width: Math.max(0, right - left),
            }}
          />
        );
      })}
      {gridReady && detailIconBounds && (
        <button
          type="button"
          className="grid-cell-details-float"
          data-testid="cell-details-float"
          title="Open row details"
          aria-label="Open row details"
          style={{
            left: detailIconBounds.x + detailIconBounds.width,
            top: detailIconBounds.y,
          }}
          onClick={openSelectedCell}
        >
          <Maximize2 size={11} aria-hidden />
        </button>
      )}
      {headerHandlers.activeGroupMenu &&
        headerHandlers.activeGroupLayout &&
        headerHandlers.activeGroupMenuBounds && (
          <NativeColumnGroupMenu
            group={headerHandlers.activeGroupMenu}
            layout={headerHandlers.activeGroupLayout}
            bounds={headerHandlers.activeGroupMenuBounds}
            onLayoutChange={columnGroups.setGroupLayout}
          />
      )}
      {/* Glide draws headers on canvas, so interactive header controls need a DOM overlay. */}
      {gridReady &&
        (hiddenBoundaries.length > 0 ||
          onAddColumnAtEnd ||
          annotationPlacements.length > 0) && (
        <div
          className="grid-hidden-columns-band"
          data-testid="grid-header-overlay-band"
          style={{ top: headerTop, height: 34 }}
        >
          {onRevealColumns &&
            hiddenBoundaries.map((boundary) => (
              <button
                key={boundary.key}
                type="button"
                className="grid-hidden-boundary"
                data-testid="grid-hidden-boundary"
                data-hidden-count={boundary.columns.length}
                data-hidden-columns={boundary.columns.join(',')}
                title={`Show ${boundary.columns.length} hidden column${
                  boundary.columns.length === 1 ? '' : 's'
                }`}
                aria-label={`Show ${boundary.columns.length} hidden column${
                  boundary.columns.length === 1 ? '' : 's'
                }`}
                style={{ left: bandX(boundary.x) }}
                onClick={() => onRevealColumns(boundary.columns)}
              >
                <span aria-hidden>{`‹${boundary.columns.length}›`}</span>
              </button>
            ))}
          {onAddColumnAtEnd && (
            <button
              type="button"
              className="grid-add-column-button"
              data-testid="grid-add-column-button"
              title="Add column"
              aria-label="Add column"
              style={{
                left:
                  hostWidth > 0
                    ? Math.min(bandX(columnsEndX) + ADD_COLUMN_BUTTON_GAP, hostWidth - 20)
                    : bandX(columnsEndX) + ADD_COLUMN_BUTTON_GAP,
              }}
              onClick={(event) => {
                const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
                onAddColumnAtEnd({ x: rect.left, y: rect.bottom });
              }}
            >
              <span aria-hidden>+</span>
            </button>
          )}
          {/* The reusable column-annotation chip(s), positioned over the
              combined header extent of their spanned columns
              (bandX-translated like every other band affordance). */}
          {annotationPlacements.map(({ annotation, leftRaw, rightRaw, headerOffset }) => {
            const left = bandX(leftRaw);
            const right = bandX(rightRaw);
            return (
              <ColumnAnnotationChip
                key={annotation.key}
                annotation={annotation}
                left={left}
                minWidth={Math.max(0, right - left)}
                headerOffset={headerOffset}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

export function SheetGrid(props: SheetGridProps) {
  const controller = useSheetGridController(props);
  return <SheetGridSurface controller={controller} />;
}
