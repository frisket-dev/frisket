// Grid header/filter/sort menus: pure presentational components + saved-view
// spec normalizers/labels.
import {
  ArrowUpDown,
  Columns3,
  Eye,
  EyeOff,
  FileDown,
  ListFilter,
  MapPin,
  Pin,
  PinOff,
  Save,
  Puzzle,
  Type,
  X,
} from 'lucide-react';
import type {
  GridFilterOperator,
  GridFilterValue,
  GridSortDirection,
} from '../api/open';
import type { ActionTemplate } from '../api/types';
import { ACTION_ICON_BY_KIND } from '../workbench/actSurface';
import { clampMaxHeightBelow } from '../hooks/useAnchoredPosition';
import { type HeaderMenuState } from './workspaceState';
import { gridFilterLabel } from './gridColumnState';

export function GridColumnHeaderMenu({
  state,
  activeSort,
  activeFilter,
  onSort,
  onClearSort,
  onFilter,
  onClearFilter,
  onOpenFilterSidebar,
  onAdvancedSort,
  columnActions,
  onColumnAction,
  actionsDisabled,
  frozenColumnCount,
  onFreezeColumns,
  onUnfreezeColumns,
  onOpenSettings,
  onSaveView,
  onOpenMap,
  mapAvailable,
  mapAvailabilityStatus,
  mapAvailabilityReason,
  onHideColumn,
  adjacentHiddenColumns,
  onUnhideColumns,
  onInsertColumnLeft,
  onInsertColumnRight,
  onExportColumnTables,
  isRowTitleColumn,
  onSetRowTitleColumn,
}: {
  state: HeaderMenuState;
  activeSort: { column: string; dir: GridSortDirection } | null;
  activeFilter: Partial<Record<GridFilterOperator, GridFilterValue>> | null;
  onSort(direction: GridSortDirection): void;
  onClearSort(): void;
  onFilter(): void;
  onClearFilter(): void;
  /** Opens filtering's single home in the Facets sidebar. */
  onOpenFilterSidebar(): void;
  onAdvancedSort(): void;
  /** Actions valid for this column's type (applicability metadata) — each
   *  opens the drawer with the column preloaded (caret contract). */
  columnActions: ActionTemplate[];
  onColumnAction(actionTemplate: ActionTemplate): void;
  actionsDisabled?: boolean;
  frozenColumnCount: number;
  onFreezeColumns(count: number): void;
  onUnfreezeColumns(): void;
  onOpenSettings(): void;
  onSaveView?: () => void;
  onOpenMap(): void;
  mapAvailable: boolean;
  mapAvailabilityStatus: string;
  mapAvailabilityReason: string;
  /** Hide this column (Google-Sheets-style collapse; data untouched). */
  onHideColumn(): void;
  /** User-hidden columns in the run(s) flanking this column; drives "Unhide N". */
  adjacentHiddenColumns: string[];
  onUnhideColumns(): void;
  onInsertColumnLeft(): void;
  onInsertColumnRight(): void;
  /** Bulk table export: offered on JSON columns
   *  (the pdf_tables/derive.table_from_list list-shaped output) so users can
   *  revisit an already-produced column, not only right after extraction. */
  onExportColumnTables(): void;
  /** Whether THIS column is the sheet's current
   *  row-title column — either the explicit override or, when unset, the
   *  resolved default (web/src/workbench/rowTitle.ts). Disables the item so
   *  it reads as a status, not a re-clickable no-op. */
  isRowTitleColumn: boolean;
  /** Sets sheets.title_column_id to this column (server-persisted). */
  onSetRowTitleColumn(): void;
}) {
  const left = Math.max(8, state.bounds.x + state.bounds.width - 246);
  const top = state.bounds.y + state.bounds.height + 4;
  // Cap the menu to the space below its anchor so every item (incl. the
  // filter-sidebar / advanced-sort rows) stays inside the viewport and reachable by
  // internal scroll.
  const maxHeight = clampMaxHeightBelow(top, 220, typeof window !== 'undefined' ? window.innerHeight : 900);
  const filterLabel = activeFilter ? gridFilterLabel({ [state.column.name]: activeFilter }) : null;
  const sortLabel = activeSort ? `${activeSort.dir === 'asc' ? 'ascending' : 'descending'}` : null;
  const freezeTargetCount = state.columnIndex + 1;
  const showUnfreezeColumns = state.columnIndex === 0 && frozenColumnCount > 0;
  const showFreezeColumns = !showUnfreezeColumns && freezeTargetCount !== frozenColumnCount;

  return (
    <div
      className="grid-header-menu"
      data-testid="grid-column-header-menu"
      role="menu"
      aria-label={`${state.column.name} column actions`}
      style={{ left, top, maxHeight }}
    >
      <div className="grid-header-menu-title">
        <span>{state.column.name}</span>
        <span>
          {state.column.type}
          {filterLabel && (
            <span className="grid-header-menu-filtered" data-testid="header-menu-filtered-badge">
              filtered
            </span>
          )}
        </span>
      </div>
      {state.column.type === 'geo_point' && (
        <button
          type="button"
          className="grid-header-menu-item"
          data-testid="header-menu-open-map"
          data-availability-status={mapAvailabilityStatus}
          data-availability-reason={mapAvailabilityReason}
          disabled={!mapAvailable}
          aria-disabled={!mapAvailable}
          onClick={onOpenMap}
        >
          <MapPin size={14} aria-hidden />
          Open map
        </button>
      )}
      {state.column.type === 'json' && (
        <button
          type="button"
          className="grid-header-menu-item"
          data-testid="header-menu-export-column-tables"
          onClick={onExportColumnTables}
        >
          <FileDown size={14} aria-hidden />
          Export tables…
        </button>
      )}
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-sort-asc"
        onClick={() => onSort('asc')}
      >
        <ArrowUpDown size={14} aria-hidden />
        Sort ascending
      </button>
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-sort-desc"
        onClick={() => onSort('desc')}
      >
        <ArrowUpDown size={14} aria-hidden />
        Sort descending
      </button>
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-clear-sort"
        disabled={!activeSort}
        onClick={onClearSort}
      >
        <X size={14} aria-hidden />
        Clear sort{sortLabel ? ` (${sortLabel})` : ''}
      </button>

      <div className="grid-header-menu-sep" />
      {/* Filter… opens the inline filter row for this column. The richer
          operator/date controls live with the friendly facets below. */}
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-filter"
        onClick={onFilter}
      >
        <ListFilter size={14} aria-hidden />
        Filter…{filterLabel ? ` (${filterLabel})` : ''}
      </button>
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-clear-filter"
        disabled={!activeFilter}
        onClick={onClearFilter}
      >
        <X size={14} aria-hidden />
        Clear filter
      </button>

      <div className="grid-header-menu-sep" />
      {/* Friendly facets are the single home for richer filtering. */}
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-filter-sidebar"
        onClick={onOpenFilterSidebar}
      >
        <ListFilter size={14} aria-hidden />
        Filter in sidebar…
      </button>
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-advanced-sort"
        onClick={onAdvancedSort}
      >
        <ArrowUpDown size={14} aria-hidden />
        Advanced sort…
      </button>

      {columnActions.length > 0 && (
        <>
          <div className="grid-header-menu-sep" />
          <div className="grid-header-menu-section" data-testid="header-menu-actions-section">
            Actions on this column
          </div>
          {/* The type-applicable action list can be long; scroll it internally
              so the sort/filter/settings/save-view items stay reachable. */}
          <div className="grid-header-menu-actions-scroll">
            {columnActions.map((template) => {
              const Icon = ACTION_ICON_BY_KIND[template.kind] ?? Puzzle;
              return (
                <button
                  key={template.kind}
                  type="button"
                  className="grid-header-menu-item"
                  data-testid={`header-menu-action-${template.kind}`}
                  disabled={actionsDisabled}
                  onClick={() => onColumnAction(template)}
                >
                  <Icon size={14} aria-hidden />
                  {template.name}
                </button>
              );
            })}
          </div>
        </>
      )}

      <div className="grid-header-menu-sep" />
      {showUnfreezeColumns && (
        <button
          type="button"
          className="grid-header-menu-item"
          data-testid="header-menu-unfreeze-columns"
          onClick={onUnfreezeColumns}
        >
          <PinOff size={14} aria-hidden />
          Unfreeze columns
        </button>
      )}
      {showFreezeColumns && (
        <button
          type="button"
          className="grid-header-menu-item"
          data-testid="header-menu-freeze-columns"
          onClick={() => onFreezeColumns(freezeTargetCount)}
        >
          <Pin size={14} aria-hidden />
          {freezeTargetCount === 1
            ? 'Freeze first column'
            : `Freeze first ${freezeTargetCount} columns`}
        </button>
      )}
      <div className="grid-header-menu-sep" />
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-insert-column-left"
        onClick={onInsertColumnLeft}
      >
        <Columns3 size={14} aria-hidden />
        Insert column left
      </button>
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-insert-column-right"
        onClick={onInsertColumnRight}
      >
        <Columns3 size={14} aria-hidden />
        Insert column right
      </button>
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-hide-column"
        onClick={onHideColumn}
      >
        <EyeOff size={14} aria-hidden />
        Hide column
      </button>
      {adjacentHiddenColumns.length > 0 && (
        <button
          type="button"
          className="grid-header-menu-item"
          data-testid="header-menu-unhide-columns"
          onClick={onUnhideColumns}
        >
          <Eye size={14} aria-hidden />
          {`Unhide ${adjacentHiddenColumns.length} column${
            adjacentHiddenColumns.length === 1 ? '' : 's'
          }`}
        </button>
      )}

      <div className="grid-header-menu-sep" />
      {/* Sets the sheet-level row-title override
          (server-persisted sheets.title_column_id) — the row drawer/document
          view/etc. all read it through the shared rowTitle helper. */}
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-use-as-row-title"
        aria-pressed={isRowTitleColumn}
        disabled={isRowTitleColumn}
        onClick={onSetRowTitleColumn}
      >
        <Type size={14} aria-hidden />
        {isRowTitleColumn ? 'Row title column' : 'Use as row title'}
      </button>
      <div className="grid-header-menu-sep" />
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-column-settings"
        onClick={onOpenSettings}
      >
        <Columns3 size={14} aria-hidden />
        Display format and settings
      </button>
      {onSaveView && (
        <button
          type="button"
          className="grid-header-menu-item"
          data-testid="header-menu-save-view"
          onClick={onSaveView}
        >
          <Save size={14} aria-hidden />
          Save current view
        </button>
      )}
    </div>
  );
}
