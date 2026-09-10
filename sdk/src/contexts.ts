// Standalone author-facing mirror, pinned against the host contract.

export interface ColumnDef {
  id: string;
  name: string;
  type: string;
}

export interface SheetSnapshot {
  id: string;
  name: string;
  rowCount: number;
  columns: ColumnDef[];
}

export interface SelectionSnapshot {
  selectedRowIds: string[];
  selectedCount: number;
  /** Open row drawer; distinct from checkbox selection. */
  activeRowId: string | null;
}

// Passed through unchanged and parity-tested against the host grid types.
export type GridFilterOperator =
  | 'eq'
  | 'in'
  | 'neq'
  | 'contains'
  | 'gte'
  | 'lte'
  | 'between'
  | 'date_relative'
  | 'date_this_year'
  | 'date_ytd'
  | 'date_year'
  | 'date_month'
  | 'date_weekday'
  | 'date_invalid'
  | 'bbox'
  | 'entity_eq'
  | 'failed'
  | 'list_contains_any';

export interface GridFilterRangeValue {
  start: string;
  end: string;
}

export interface GridFilterRelativeDateValue {
  amount: number;
  unit: 'days' | 'weeks' | 'months';
}

export interface GridFilterBboxValue {
  min_lon: number;
  min_lat: number;
  max_lon: number;
  max_lat: number;
}

/** Entity filter: `type` alone, or with exactly one of exact `text` and
 * deterministic-variant `fingerprint`. */
export interface GridFilterEntityValue {
  type: string;
  text?: string;
  fingerprint?: string;
}

/** One server-authored member selector for a list-valued JSON-column filter. */
export type GridFilterListSelector =
  | { kind: 'scalar'; value: string | number | boolean }
  | { kind: 'entity'; type: string; text: string };

export type GridFilterValue =
  | string
  | string[]
  | GridFilterRangeValue
  | GridFilterRelativeDateValue
  | GridFilterBboxValue
  | GridFilterEntityValue
  | GridFilterListSelector[];

/** Column name to operator/value filters. */
export type GridFilterSpec = Record<string, Partial<Record<GridFilterOperator, GridFilterValue>>>;

export type GridSortDirection = 'asc' | 'desc';

export interface GridSortRule {
  column: string;
  dir: GridSortDirection;
}

export type GridSortSpec = GridSortRule[];

/** Present only with `grid.state.read`. */
export interface GridReadFragment {
  filter: GridFilterSpec | null;
  sort: GridSortSpec | null;
  visibleColumnIds: string[];
  frozenColumnCount: number;
  activeLensId: number | null;
}

/** Present only with `action.run`; `run` awaits the host cost gate. */
export interface ActionLaunchFragment {
  run(actionKind: string): Promise<{ launched: boolean }>;
  preview(actionKind: string): Promise<{ launched: boolean }>;
}

/** Present only with `grid.filter.applyBbox`; the host resolves `columnId`
 * to the canonical column-name bbox filter. */
export interface GridFilterApplyFragment {
  applyBbox(columnId: string, bbox: [number, number, number, number]): void;
}

/** Minimal host-injected `@deck.gl/core` surface. Constructor props are not
 * pinned; use deck.gl's own types when needed. */
export interface DeckglNamespace {
  Deck: new (props: Record<string, unknown>) => {
    setProps(props: Record<string, unknown>): void;
    finalize(): void;
  };
  WebMercatorViewport: new (props?: Record<string, unknown>) => unknown;
  ScatterplotLayer: new (props: Record<string, unknown>) => unknown;
  BitmapLayer: new (props: Record<string, unknown>) => unknown;
  TileLayer: new (props: Record<string, unknown>) => unknown;
  HeatmapLayer: new (props: Record<string, unknown>) => unknown;
}

/** Present only with `host.library.deckgl`; never defaulted. The host loads
 * the shared namespace lazily, so plugins must await it. */
export interface HostLibraryFragment {
  deckgl: Promise<DeckglNamespace>;
}

interface PanelShapedContextBase {
  projectId: string;
  contributionId: string;
  sheet: SheetSnapshot;
  selection: SelectionSnapshot;
  grid: {
    filter: GridFilterSpec | null;
    sort: GridSortSpec | null;
  };
  navigation: {
    openRow(rowId: string): void;
  };
  gridState?: GridReadFragment;
  actions?: ActionLaunchFragment;
}

export interface PluginPanelContext extends PanelShapedContextBase {
  schemaVersion: 'frisket.plugin_panel_context.v1';
  placement: {
    host: 'rightInspector' | 'leftSidebar';
    mode: 'panel';
  };
}

export interface PluginDockTabContext extends PanelShapedContextBase {
  schemaVersion: 'frisket.plugin_dock_tab_context.v1';
  placement: {
    host: 'bottomDock';
    mode: 'tab';
  };
  dock: {
    isActiveTab: boolean;
    focus(): void;
  };
}

export type PluginDetailSubject =
  | { kind: 'row'; sheetId: string; rowId: string }
  | { kind: 'column'; sheetId: string; columnId: string; columnName: string }
  | { kind: 'entity'; entityId: string; label: string }
  | { kind: 'source'; sourceId: string };

export interface PluginDetailContext extends PanelShapedContextBase {
  schemaVersion: 'frisket.plugin_detail_context.v1';
  detail: {
    subject: PluginDetailSubject;
  };
}

export interface PluginPeekContext extends PanelShapedContextBase {
  schemaVersion: 'frisket.plugin_peek_context.v1';
  peek: {
    close(): void;
  };
}

/** Fresh snapshot for each palette invocation. */
export interface PluginCommandContext {
  schemaVersion: 'frisket.plugin_command_context.v1';
  projectId: string;
  contributionId: string;
  commandId: string;
  sheet: SheetSnapshot | null;
  selection: SelectionSnapshot;
  navigation: { openRow(rowId: string): void } | null;
  peek: { open(contributionId: string): void } | null;
  gridState?: GridReadFragment;
  actions?: ActionLaunchFragment;
}

export interface PluginViewContext {
  schemaVersion: 'frisket.plugin_view_context.v1';
  projectId: string;
  contributionId: string;
  placement: {
    host: 'mainView';
    mode: 'pane';
  };
  sheet: SheetSnapshot;
  /** Live workspace selection; always present. */
  selection: SelectionSnapshot;
  rows: {
    query(args: {
      columnIds?: string[];
      offset: number;
      limit: number;
    }): Promise<{ rows: unknown[]; total: number }>;
  };
  media: {
    fromCell(value: unknown): unknown | null;
  };
  navigation: {
    openRow(rowId: string): void;
  };
  gridState?: GridReadFragment;
  gridFilter?: GridFilterApplyFragment;
  actions?: ActionLaunchFragment;
  libs?: HostLibraryFragment;
}

/** Projection read parameters. The host applies the current grid filter/sort. */
export interface ProjectionDataReadParams {
  columnId: string;
  bbox?: [number, number, number, number];
  attrs?: string[];
}

export interface PluginProjectionViewContext {
  schemaVersion: 'frisket.plugin_projection_view_context.v1';
  projectId: string;
  contributionId: string;
  placement: {
    host: 'mainView';
    mode: 'pane';
  };
  sheet: SheetSnapshot;
  /** Same live selection as `PluginViewContext.selection`. */
  selection: SelectionSnapshot;
  projection: {
    kind: string;
    target: Record<string, unknown>;
    params: Record<string, unknown>;
    status(): Promise<unknown>;
    build(args?: { mode?: 'refresh' | 'rebuild' }): Promise<unknown>;
    readArtifact(ref: { artifactId: string }): Promise<unknown>;
    /** Present only with `projection.data.read`; returns host-owned Arrow IPC. */
    fetchData?(params: ProjectionDataReadParams): Promise<ArrayBuffer>;
  };
  navigation: {
    openRow(rowId: string): void;
    /** Present only when the host route can dismiss this view. */
    closeView?(): void;
  };
  gridState?: GridReadFragment;
  /** Present only with `grid.filter.applyBbox`. */
  gridFilter?: GridFilterApplyFragment;
  actions?: ActionLaunchFragment;
  libs?: HostLibraryFragment;
}

export type PluginHostContext =
  | PluginPanelContext
  | PluginDockTabContext
  | PluginDetailContext
  | PluginPeekContext
  | PluginCommandContext
  | PluginViewContext
  | PluginProjectionViewContext;

/** Minimal host-injected React surface; cast to React's types when needed. */
export interface ReactRuntime {
  createElement(...args: unknown[]): unknown;
  Fragment: unknown;
  useState(...args: unknown[]): unknown;
  useEffect(...args: unknown[]): unknown;
  useMemo(...args: unknown[]): unknown;
  useCallback(...args: unknown[]): unknown;
}

export interface PluginComponentProps<Ctx extends PluginHostContext = PluginHostContext> {
  React: ReactRuntime;
  ctx: Ctx;
}
