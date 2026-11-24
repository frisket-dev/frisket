import './gridMenus.css';
import { useEffect, useRef } from 'react';
import { MenuPop } from '../components/MenuPop';
import { useNativePopover } from '../hooks/useNativePopover';
import { resolveAnchoredPosition } from '../hooks/useAnchoredPosition';
// Grid header/filter/sort menus: pure presentational components + saved-view
// spec normalizers/labels.
import {
  ArrowDown,
  ArrowUp,
  Columns3,
  Eye,
  EyeOff,
  FileDown,
  GitCompareArrows,
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
import type {
  ActionColumnMenuEntry,
  ActionRunEntryCallbacks,
} from '../grid/actionRunEntry';
import { type HeaderMenuState } from './workspaceState';

export function GridColumnHeaderMenu({
  state,
  activeSort,
  activeFilter,
  onSort,
  onClearSort,
  filterLabel,
  onFilter,
  onClearFilter,
  columnActions,
  onColumnAction,
  actionsDisabled,
  actionRunEntry,
  onCompareActionRun,
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
  /** `Filter…` for a faceted column; `Search in <column>…` for long text. */
  filterLabel: string;
  /** Opens the Filter sidebar on this column's facet or scoped search. */
  onFilter(): void;
  onClearFilter(): void;
  /** Actions valid for this column's type (applicability metadata) — each
   *  opens the drawer with the column preloaded (caret contract). */
  columnActions: ActionTemplate[];
  onColumnAction(actionTemplate: ActionTemplate): void;
  actionsDisabled?: boolean;
  /** Catalog-gated generated-column entry. Its anchor identifies the complete
   * output family; menu actions must not operate on only the visible column. */
  actionRunEntry?: ActionColumnMenuEntry | null;
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
} & ActionRunEntryCallbacks) {
  const left = Math.max(8, state.bounds.x + state.bounds.width - 240);
  const top = state.bounds.y + state.bounds.height + 4;
  // Cap the menu to the space below its anchor so every item stays inside the
  // viewport and reachable by internal scroll.
  const maxHeight = clampMaxHeightBelow(top, 220, typeof window !== 'undefined' ? window.innerHeight : 900);
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
        <span className="grid-header-menu-type">{state.column.type}</span>
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
        aria-pressed={activeSort?.dir === 'asc'}
        onClick={() => onSort('asc')}
      >
        <ArrowUp size={14} aria-hidden />
        Sort ascending
      </button>
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-sort-desc"
        aria-pressed={activeSort?.dir === 'desc'}
        onClick={() => onSort('desc')}
      >
        <ArrowDown size={14} aria-hidden />
        Sort descending
      </button>
      {activeSort && (
        <button
          type="button"
          className="grid-header-menu-item"
          data-testid="header-menu-clear-sort"
          onClick={onClearSort}
        >
          <X size={14} aria-hidden />
          Clear sort
        </button>
      )}

      <div className="grid-header-menu-sep" />
      <button
        type="button"
        className="grid-header-menu-item"
        data-testid="header-menu-filter"
        onClick={onFilter}
      >
        <ListFilter size={14} aria-hidden />
        {filterLabel}
      </button>
      {activeFilter && (
        <button
          type="button"
          className="grid-header-menu-item"
          data-testid="header-menu-clear-filter"
          onClick={onClearFilter}
        >
          <X size={14} aria-hidden />
          Clear filter
        </button>
      )}

      {actionRunEntry?.comparisonKind === 'row_overlay' && (
        <>
          <div className="grid-header-menu-sep" />
          <div className="grid-header-menu-action-origin" data-testid="header-menu-action-run-section">
            <span>{actionRunEntry.actionTitle}</span>
          </div>
          {onCompareActionRun && (
            <button
              type="button"
              className="grid-header-menu-item"
              data-testid="header-menu-compare-action-run"
              onClick={() => onCompareActionRun(actionRunEntry.anchor)}
            >
              <GitCompareArrows size={14} aria-hidden />
              Compare settings…
            </button>
          )}
        </>
      )}

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


export function GridRowMenu({
  bounds, rowIndex, rowCount, canSplit, selectionLoaded,
  onOpen, onSplit, onOcr, onCopy, onDelete, onClose,
}: {
  bounds: { x: number; y: number; width: number; height: number };
  rowIndex: number;
  rowCount: number;
  canSplit: boolean;
  selectionLoaded: boolean;
  onOpen(): void;
  onSplit(): void;
  onOcr(): void;
  onCopy(): void;
  onDelete(): void;
  onClose(): void;
}) {
  const menuRef = useRef<HTMLDivElement>(null);
  useNativePopover(menuRef, onClose, { focusRestore: true });
  useEffect(() => {
    menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]:not(:disabled)')?.focus();
  }, []);
  const position = resolveAnchoredPosition(new DOMRect(bounds.x, bounds.y, bounds.width, bounds.height), { width: 280, gap: 4 });
  const rows = rowCount > 1 ? `${rowCount} rows` : 'row';
  const unavailable = selectionLoaded ? undefined : 'Load and reselect these rows first.';
  return <MenuPop ref={menuRef} sizing="anchored" aria-label="Row actions"
    style={{ position: 'fixed', inset: 'auto', margin: 0, ...position }}
    onKeyDown={(event) => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'));
      if (!items.length) return;
      event.preventDefault();
      const current = items.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next].focus();
    }}>
    <div className="grid-header-menu-title">{rowCount > 1 ? `${rowCount} rows selected` : `Row ${rowIndex + 1}`}</div>
    <button type="button" role="menuitem" className="menu-item" onClick={onOpen}>Open in Document view</button>
    <div className="menu-sep" role="separator" />
    <button type="button" role="menuitem" className="menu-item" disabled={!canSplit} onClick={onSplit}>Split PDF{rowCount > 1 ? 's' : ''}…</button>
    <button type="button" role="menuitem" className="menu-item" disabled={!selectionLoaded} title={unavailable} onClick={onOcr}>OCR {rows}</button>
    <button type="button" role="menuitem" className="menu-item" disabled={!selectionLoaded} title={unavailable} onClick={onCopy}>Copy {rows}</button>
    <button type="button" role="menuitem" className="menu-item menu-item-danger" disabled={!selectionLoaded} title={unavailable} onClick={onDelete}>Delete {rows}</button>
  </MenuPop>;
}
