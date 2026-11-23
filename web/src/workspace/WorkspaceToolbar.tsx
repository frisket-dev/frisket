import './WorkspaceToolbar.css';
import { Button } from '../components/Button';
import { ActionPreviewBanner } from '../components/ActionPreviewBanner';
import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Columns3, Eye, FileText, Images, LayoutGrid, ListFilter, MapPin, MessageSquareQuote, Network, Plus, Rows3, WrapText, X, type LucideIcon } from 'lucide-react';
import { AnchoredMenu } from '../components/MenuPop';
import { SegmentedToggle } from '../components/PanelPrimitives';
import { type WorkViewKind } from './useWorkspaceChromeState';
import { DeleteRowsButton } from '../components/TabStripButtons';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useCurrentViewSelection } from '../bind/useCurrentViewSelection';
import { useSelector } from '../bind/useSelector';
import { useMainViewModel } from './WorkspaceContext';
import { FILTER_SIDEBAR_ID, FILTER_TOGGLE_TEST_ID } from './FilterSidebar';
import { deriveRowFilterChips, sheetCountLabel, withColumnContains, withColumnFacet, type RowFilterChip } from './rowFiltering';

const WORK_VIEW_SWITCH: Array<{ kind: WorkViewKind; label: string; Icon: LucideIcon }> = [
  { kind: 'grid', label: 'Grid', Icon: LayoutGrid },
  { kind: 'document', label: 'Document', Icon: FileText },
  { kind: 'extract', label: 'Extract', Icon: FileText },
  { kind: 'answers', label: 'Answers', Icon: MessageSquareQuote },
  { kind: 'map', label: 'Map', Icon: MapPin },
  { kind: 'gallery', label: 'Gallery', Icon: Images },
  { kind: 'graph', label: 'Graph', Icon: Network },
];

function WorkViewSwitcher({
  active,
  segments,
  disabledReasons,
  onSelect,
}: {
  active: WorkViewKind;
  segments: Record<WorkViewKind, boolean>;

  disabledReasons?: Partial<Record<WorkViewKind, string>>;
  onSelect: (kind: WorkViewKind) => void;
}) {
  const options: Array<{
    value: WorkViewKind;
    label: string;
    icon: LucideIcon;
    disabledReason: string | undefined;
  }> = [];
  for (const segment of WORK_VIEW_SWITCH) {
    if (!segments[segment.kind] && !disabledReasons?.[segment.kind]) continue;
    options.push({
      value: segment.kind,
      label: segment.label,
      icon: segment.Icon,
      disabledReason: segments[segment.kind] ? undefined : disabledReasons?.[segment.kind],
    });
  }
  return (
    <SegmentedToggle
      className="segmented-toolbar"
      size="compact"
      fullWidth={false}
      testId="view-switcher"
      ariaLabel="View"
      value={active}
      onValueChange={(value) => onSelect(value as WorkViewKind)}
      buttonTestId={(value) => `view-switch-${value}`}
      options={options}
    />
  );
}

function useRowFilterChips(): RowFilterChip[] {
  const { sheet } = useMainViewModel();
  const gridView = useGridViewHandle();
  const applied = useSelector(gridView.store, (s) => s.applied);
  const selection = useCurrentViewSelection(sheet);
  return deriveRowFilterChips({
    selection,
    filter: applied.filter,
    filterValueLabel: applied.filterValueLabel,
    sort: applied.sort,
    scopeRowIds: applied.scopeRowIds ?? null,
  });
}

function AddMenu() {
  const { appendRow, openAddColumnPrompt } = useMainViewModel();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const frame = requestAnimationFrame(() => {
      menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [open]);
  return (
    <>
      <button
        type="button"
        ref={triggerRef}
        className={`btn sheet-toolbar-button${open ? ' is-open' : ''}`}
        data-testid="add-menu-button"
        aria-label="Add"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Plus size={15} aria-hidden />
        <ChevronDown size={12} aria-hidden />
      </button>
      {open && (
        <AnchoredMenu
          anchorRef={triggerRef}
          menuRef={menuRef}
          onClose={() => setOpen(false)}
          width={170}
          align="right"
          className="sheet-add-menu"
          aria-label="Add"
          data-testid="add-menu"
        >
          <button
            type="button"
            role="menuitem"
            className="menu-item"
            data-testid="add-row-button"
            onClick={() => {
              setOpen(false);
              appendRow();
            }}
          >
            <Rows3 size={15} aria-hidden />
            <span className="menu-item-name">Add row</span>
          </button>
          <button
            type="button"
            role="menuitem"
            className="menu-item"
            data-testid="add-column-button"
            onClick={() => {
              const bounds = triggerRef.current?.getBoundingClientRect();
              setOpen(false);
              openAddColumnPrompt({ x: bounds ? bounds.right - 260 : 0, y: bounds ? bounds.bottom + 4 : 0 });
            }}
          >
            <Columns3 size={15} aria-hidden />
            <span className="menu-item-name">Add column…</span>
          </button>
        </AnchoredMenu>
      )}
    </>
  );
}

export function WorkspaceSheetToolbar() {
  const {
    requestDeleteRows,
    selectedRowIdsForSheet,
    sheet,
    toggleWrapText,
    activeWorkView,
    workViewSegments,
    workViewDisabledReasons,
    setWorkView,
    visibleRowCount,
    activeHiddenColumns,
    revealAllHiddenColumns,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const wrapText = useSelector(gridView.store, (s) => s.wrapText);
  const sidebar = useSelector(gridView.store, (s) => s.sidebar);
  const chips = useRowFilterChips();
  if (!sheet) return null;

  const filterOpen = sidebar.open && sidebar.tab === 'filter';
  return (
    <div className="workspace-toolbar">
      <div className="sheet-heading">
        <span className="sheet-title">{sheet.name}</span>
        {sheet.parent && (
          <span className="sheet-breadcrumb" data-testid="sheet-breadcrumb">
            ← {sheet.parent.sheetName} via <em>{sheet.parent.viaAction}</em>
          </span>
        )}
        <span className="sheet-stats" data-testid="sheet-stats">
          {sheetCountLabel(visibleRowCount, sheet.rowCount, sheet.columns.length)}
        </span>
        <div className="sheet-view-switch">
          <WorkViewSwitcher
            active={activeWorkView}
            segments={workViewSegments}
            disabledReasons={workViewDisabledReasons}
            onSelect={setWorkView}
          />
        </div>
        {activeWorkView !== 'grid' &&
          activeWorkView !== 'document' &&
          activeWorkView !== 'extract' &&
          activeWorkView !== 'answers' && (
          <span className="work-split-hint" data-testid="work-view-split-hint">
            · split view
          </span>
        )}
      </div>
      <div className="toolbar-actions">
        <DeleteRowsButton count={selectedRowIdsForSheet.length} onClick={requestDeleteRows} />
        {activeHiddenColumns.length > 0 && (
          <button
            type="button"
            className="icon-btn"
            data-testid="toolbar-show-hidden-columns"
            aria-label={`Show ${activeHiddenColumns.length} hidden column${activeHiddenColumns.length === 1 ? '' : 's'}`}
            title={`Show ${activeHiddenColumns.length} hidden column${activeHiddenColumns.length === 1 ? '' : 's'}`}
            onClick={revealAllHiddenColumns}
          >
            <Eye size={15} aria-hidden />
          </button>
        )}
        <AddMenu />
        <button
          type="button"
          className={`btn sheet-toolbar-button sheet-filter-toggle${filterOpen ? ' is-active' : ''}`}
          data-testid={FILTER_TOGGLE_TEST_ID}
          aria-expanded={filterOpen}
          aria-controls={filterOpen ? FILTER_SIDEBAR_ID : undefined}
          aria-label={chips.length > 0 ? `Filter, ${chips.length} active` : 'Filter'}
          onClick={() => (filterOpen ? gridView.closeSidebar() : gridView.openSidebar('filter'))}
        >
          <ListFilter size={15} aria-hidden />
          <span>Filter</span>
          {chips.length > 0 && (
            <span className="sheet-filter-badge" data-testid="filter-badge" aria-hidden>{chips.length}</span>
          )}
        </button>
        <button
          type="button"
          className={`icon-btn sheet-wrap-toggle${wrapText ? ' icon-btn-active' : ''}`}
          data-testid="toggle-wrap"
          title={wrapText ? 'Wrap text: on' : 'Wrap text: off'}
          aria-label="Wrap text"
          aria-pressed={wrapText}
          onClick={toggleWrapText}
        >
          <WrapText size={15} />
        </button>
      </div>
    </div>
  );
}

/** Shows what is active, never inputs: filter chips hide rows, shaping chips
 *  (sort, limit) do not. A chip's label opens its control in the sidebar. */
export function WorkspaceGridControls() {
  const {
    sheet,
    applyRowSearch,
    applyRowLimit,
    applyRowOrder,
    applyGridFilterSpec,
    clearViewSelection,
    canCreateView,
    openNewView,
    updateViewSelection,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const filter = useSelector(gridView.store, (s) => s.applied.filter);
  const selection = useCurrentViewSelection(sheet);
  const chips = useRowFilterChips();
  if (!sheet || chips.length === 0) return null;

  const openChip = (chip: RowFilterChip) => {
    const target = chip.target;
    if (target.kind === 'search') gridView.openSidebar('filter', { search: true });
    else if (target.kind === 'facet') gridView.openSidebar('filter', { facet: target.column });
    else gridView.openSidebar('filter');
  };
  const removeChip = (chip: RowFilterChip) => {
    const target = chip.target;
    if (target.kind === 'search') applyRowSearch({ text: '', scope: 'all' }, sheet.id);
    else if (target.kind === 'facet') applyGridFilterSpec(withColumnFacet(filter, target.column, null));
    else if (target.kind === 'contains') applyGridFilterSpec(withColumnContains(filter, target.column, null));
    else if (target.kind === 'sort') applyRowOrder(null);
    else if (target.kind === 'limit') applyRowLimit(null);
    else if (target.kind === 'rows') {
      gridView.store.set((state) => ({
        ...state,
        activeViewId: null,
        applied: { ...state.applied, scopeRowIds: null },
      }));
    } else if (selection) {
      updateViewSelection({
        ...selection,
        similarity: undefined,
        order: selection.order.filter((item) => !('score' in item && item.score === 'similarity')),
      });
    }
  };
  const firstShape = chips.findIndex((chip) => chip.tone === 'shape');

  return (
    <div className="row-filter-chips" data-testid="row-filter-chips" role="region" aria-label="Active filters">
      <ul className="row-filter-chip-list">
        {chips.map((chip, index) => (
          <li key={chip.key} className="row-filter-chip-item">
            {index === firstShape && index > 0 && <span className="row-filter-chip-divider" aria-hidden />}
            <span className="row-filter-chip" data-tone={chip.tone} data-testid={`row-filter-chip-${chip.key}`}>
              <button
                type="button"
                className="row-filter-chip-label"
                onClick={() => openChip(chip)}
              >
                <span className="row-filter-chip-kind">{chip.kind}</span>{' '}
                <span className="row-filter-chip-value">{chip.value}</span>
              </button>
              <button
                type="button"
                className="row-filter-chip-remove"
                data-testid={`row-filter-chip-remove-${chip.key}`}
                aria-label={`Remove ${chip.kind} ${chip.value}`}
                onClick={() => removeChip(chip)}
              >
                <X size={11} aria-hidden />
              </button>
            </span>
          </li>
        ))}
      </ul>
      <div className="row-filter-chip-actions">
        {canCreateView && (
          <button
            type="button"
            className="row-filter-save-view"
            data-testid="save-as-view"
            onClick={() => openNewView()}
          >
            Save as view
          </button>
        )}
        <Button type="button" data-testid="clear-all-filters" onClick={clearViewSelection}>
          Clear all
        </Button>
      </div>
    </div>
  );
}

export function WorkspaceGridBanners() {
  const {
    activeChildFilter,
    activePreviewView,
    clearChildFilter,
    closePreviewView,
    runPreviewForReal,
    sheet,
  } = useMainViewModel();
  if (!sheet && !activePreviewView) return null;

  return (
<>
              {activeChildFilter && sheet && (
                <div className="child-filter-banner" data-testid="child-filter-banner">
                  <ListFilter size={13} aria-hidden />
                  <span>
                    Showing <strong>{activeChildFilter.count.toLocaleString()}</strong>{' '}
                    {activeChildFilter.count === 1 ? 'row' : 'rows'} derived from{' '}
                    <strong>{activeChildFilter.parentSheetName}</strong>
                    {activeChildFilter.parentRowIndex != null && (
                      <> row {activeChildFilter.parentRowIndex + 1}</>
                    )}
                  </span>
                  <Button
                    type="button"

                    data-testid="child-filter-clear"
                    onClick={() => clearChildFilter()}
                  >
                    Show all {sheet.rowCount.toLocaleString()} rows
                  </Button>
                </div>
              )}

              {activePreviewView && <ActionPreviewBanner view={activePreviewView}
                onRun={runPreviewForReal} onClose={closePreviewView} />}
  </>
  );
}
