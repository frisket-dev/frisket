import './ViewsPanel.css';
import './WorkspaceToolbar.css';
import { Button } from '../components/Button';
import { TextInput } from '../components/FormControls';
import { ActionPreviewBanner } from '../components/ActionPreviewBanner';
import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import { PanelSelect } from '../components/PanelSelect';
import { BookOpen, Boxes, Eye, FileText, Images, LayoutGrid, ListFilter, MapPin, MessageSquareQuote, MoreHorizontal, Network, Plus, Rows3, ShieldCheck, WrapText, type LucideIcon } from 'lucide-react';
import { useNativePopover } from '../hooks/useNativePopover';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import { countLabel } from '../format';
import { SegmentedToggle } from '../components/PanelPrimitives';
import { type WorkViewKind } from './useWorkspaceChromeState';
import { DeleteRowsButton } from '../components/TabStripButtons';
import { ROW_HEIGHTS } from './workspaceState';
import { gridFilterSummaryLabel, gridSortLabel } from './gridColumnState';
import { GridSortPanel } from './GridSortPanel';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { selectInlineFilterColumn, selectInlineFilterOpen } from '../state/gridViewStore';
import { useSelector } from '../bind/useSelector';
import { useMainViewModel } from './WorkspaceContext';

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

export function WorkspaceInlineFilterRow() {
  const {
    applyInlineFilter,
    sheet,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const activeGridFilter = useSelector(gridView.store, (s) => s.applied.filter);
  const inlineFilterOpen = useSelector(gridView.store, (s) =>
    selectInlineFilterOpen(s, sheet?.id),
  );
  const inlineFilterColumn = useSelector(gridView.store, (s) =>
    selectInlineFilterColumn(s, sheet?.id),
  );
  if (!sheet) return null;
  const column = inlineFilterColumn || sheet.columns[0]?.name || '';
  const activeEntry = activeGridFilter?.[column]?.contains;

  const activeText = typeof activeEntry === 'string' ? activeEntry : '';
  if (!inlineFilterOpen) return null;
  return (
    <div className="inline-filter-row" data-testid="inline-filter-row">
      <span className="inline-filter-label mono">Filter</span>
      <PanelSelect
        className="row-height-select"
        data-testid="inline-filter-column"
        aria-label="Filter column"
        value={column}
        onChange={(event) => {
          gridView.setInlineFilterColumn(sheet.id, event.target.value);
        }}
      >
        {sheet.columns.map((col) => (
          <option key={col.id} value={col.name}>
            {col.name}
          </option>
        ))}
      </PanelSelect>
      <input
        className="inline-filter-input"
        data-testid="inline-filter-input"
        aria-label="Filter value"
        placeholder="contains…"
        value={activeText}
        onChange={(event) => {
          applyInlineFilter(column, event.target.value);
        }}
      />
      <button
        type="button"
        className="pill-btn"
        data-testid="inline-filter-clear"
        onClick={() => {
          applyInlineFilter(column, '');
        }}
      >
        clear
      </button>
      <button
        type="button"
        className="pill-btn"
        data-testid="inline-filter-done"
        onClick={gridView.closeInlineFilter}
      >
        done
      </button>
    </div>
  );
}

export function WorkspaceSheetToolbar() {
  const {
    appendRow,
    requestDeleteRows,
    selectedRowIdsForSheet,
    sheet,
    toggleWrapText,
    activeWorkView,
    workViewSegments,
    workViewDisabledReasons,
    setWorkView,
    visibleRowCount,
    canCreateSavedView,
    openNewSavedView,
    openSavedViews,
    activeSavedViewId,
    views,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const wrapText = useSelector(gridView.store, (s) => s.wrapText);
  if (!sheet) return null;

  const selectedRowCount = selectedRowIdsForSheet.length;

  return (
<>
              <div className="workspace-toolbar">
                <div className="sheet-heading">
                  <span className="sheet-title">{sheet.name}</span>
                  {sheet.parent && (
                    <span className="sheet-breadcrumb" data-testid="sheet-breadcrumb">
                      ← {sheet.parent.sheetName} via <em>{sheet.parent.viaAction}</em>
                    </span>
                  )}
                  <span className="muted" data-testid="sheet-stats">
                    {countLabel(visibleRowCount, 'row')} · {countLabel(sheet.columns.length, 'column')}
                  </span>
                  {activeSavedViewId !== null && views.find((view) => view.id === activeSavedViewId) && (
                    <button
                      type="button"
                      className="active-saved-view-chip"
                      data-testid="active-saved-view-chip"
                      onClick={openSavedViews}
                    >
                      Active view: {views.find((view) => view.id === activeSavedViewId)!.name}
                    </button>
                  )}
                  <WorkViewSwitcher
                    active={activeWorkView}
                    segments={workViewSegments}
                    disabledReasons={workViewDisabledReasons}
                    onSelect={setWorkView}
                  />
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
                  <button
                    type="button"
                    className="icon-btn"
                    data-testid="add-row-button"
                    title="Add row"
                    onClick={appendRow}
                  >
                    <Plus size={15} />
                  </button>

                  <DeleteRowsButton count={selectedRowCount} onClick={requestDeleteRows} />
                  <div className="toolbar-spacer" />

                  {canCreateSavedView && (
                    <button
                      type="button"
                      className="icon-btn"
                      data-testid="save-as-view"
                      title="Save current view"
                      aria-label="Save current view"
                      onClick={() => openNewSavedView()}
                    >
                      <BookOpen size={15} />
                    </button>
                  )}

                  <button
                    type="button"
                    className={`icon-btn${wrapText ? ' icon-btn-active' : ''}`}
                    data-testid="toggle-wrap"
                    title={wrapText ? 'Wrap text: on' : 'Wrap text: off'}
                    aria-label="Wrap text"
                    aria-pressed={wrapText}
                    onClick={toggleWrapText}
                  >
                    <WrapText size={15} />
                  </button>
                  <ToolbarOverflowMenu />
                </div>
              </div>
  </>
  );
}

interface SavedViewsSubmenuPosition {
  top: number;
  left: number;
  width: number;
  maxHeight: number;
}

function useSavedViewsSubmenuPosition(
  anchorRef: RefObject<HTMLElement | null>,
  enabled: boolean,
): SavedViewsSubmenuPosition | null {
  const [position, setPosition] = useState<SavedViewsSubmenuPosition | null>(null);

  useLayoutEffect(() => {
    if (!enabled) return undefined;
    const measure = () => {
      const anchor = anchorRef.current;
      if (!anchor) return;
      const margin = 8;
      const gap = 4;
      const width = Math.max(0, Math.min(280, window.innerWidth - margin * 2));
      const maxHeight = Math.max(0, Math.min(320, window.innerHeight - margin * 2));
      const rect = anchor.getBoundingClientRect();
      const rightwardLeft = rect.right + gap;
      const preferredLeft = rightwardLeft + width <= window.innerWidth - margin
        ? rightwardLeft
        : rect.left - gap - width;
      const left = Math.max(margin, Math.min(preferredLeft, window.innerWidth - margin - width));
      const top = Math.max(margin, Math.min(rect.top, window.innerHeight - margin - maxHeight));
      setPosition({ top, left, width, maxHeight });
    };
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [anchorRef, enabled]);

  return position;
}

function ToolbarOverflowMenu() {
  const {
    activeHiddenColumns,
    changeRowHeight,
    overflowMenuOpen,
    setOverflowMenuOpen,
    provenanceOpen,
    toggleProvenanceWithDrawers,
    revealAllHiddenColumns,
    canCreateSavedView,
    ensureViewsLoaded,
    openSavedViews,
    openNewSavedView,
    applySavedView,
    views,
    activeSavedViewId,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const rowHeight = useSelector(gridView.store, (s) => s.rowHeight);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const rowHeightMenuRef = useRef<HTMLDivElement>(null);
  const savedViewsTriggerRef = useRef<HTMLButtonElement>(null);
  const savedViewsMenuRef = useRef<HTMLDivElement>(null);
  const savedViewsLoadRequestedRef = useRef(false);
  const suppressNextSavedViewsFocusOpenRef = useRef(false);
  const [savedViewsMenuOpen, setSavedViewsMenuOpen] = useState(false);
  const [savedViewsQuery, setSavedViewsQuery] = useState('');
  const [applyAnnouncement, setApplyAnnouncement] = useState('');

  const closeSavedViewsSubmenu = useCallback(() => {
    savedViewsLoadRequestedRef.current = false;
    setSavedViewsMenuOpen(false);
    setSavedViewsQuery('');
  }, []);

  const openSavedViewsSubmenu = useCallback(() => {
    if (savedViewsLoadRequestedRef.current) return;
    savedViewsLoadRequestedRef.current = true;
    setSavedViewsMenuOpen(true);
    void ensureViewsLoaded();
  }, [ensureViewsLoaded]);

  const setOverflowOpen = useCallback(
    (next: boolean) => {
      setOverflowMenuOpen(next);
      if (!next) {
        closeSavedViewsSubmenu();
      }
    },
    [closeSavedViewsSubmenu, setOverflowMenuOpen],
  );

  useNativePopover(menuRef, () => setOverflowOpen(false), {
    enabled: overflowMenuOpen,
    ignoreSelector: '[data-testid="toolbar-overflow-button"]',
    extraRefs: [rowHeightMenuRef, savedViewsMenuRef],
  });

  useNativePopover(savedViewsMenuRef, (reason) => {
    if (reason === 'escape') suppressNextSavedViewsFocusOpenRef.current = true;
    closeSavedViewsSubmenu();
  }, {
    enabled: overflowMenuOpen && savedViewsMenuOpen,
    focusRestore: savedViewsTriggerRef,
    sourceRef: savedViewsTriggerRef,
    ignoreSelector: '[data-testid="open-saved-views-submenu"]',
  });

  const menuPos = useAnchoredPosition(triggerRef, {
    enabled: overflowMenuOpen,
    align: 'right',
    width: 210,
    gap: 4,
  });
  const savedViewsMenuPos = useSavedViewsSubmenuPosition(
    menuRef,
    overflowMenuOpen && savedViewsMenuOpen,
  );
  const normalizedViewsQuery = savedViewsQuery.trim().toLocaleLowerCase();
  const filteredViews = normalizedViewsQuery
    ? views.filter((view) => view.name.toLocaleLowerCase().includes(normalizedViewsQuery))
    : views;

  return (
    <div className="toolbar-overflow" ref={rootRef}>
      <button
        type="button"
        ref={triggerRef}
        className={`icon-btn${overflowMenuOpen ? ' icon-btn-active' : ''}`}
        data-testid="toolbar-overflow-button"
        title="More view options"
        aria-label="More view options"
        aria-haspopup="menu"
        aria-expanded={overflowMenuOpen}
        onClick={() => setOverflowOpen(!overflowMenuOpen)}
      >
        <MoreHorizontal size={15} />
      </button>
      {overflowMenuOpen && (
        <div
          ref={menuRef}
          className="menu-pop toolbar-overflow-menu"
          data-testid="toolbar-overflow-menu"
          role="menu"
          style={
            menuPos
              ? { position: 'fixed', inset: 'auto', top: menuPos.top, bottom: menuPos.bottom, left: menuPos.left, right: 'auto', width: menuPos.width, margin: 0 }
              : { position: 'fixed', visibility: 'hidden' }
          }
        >
          <div className="menu-item menu-item-select" title="Row height (applies to all rows)">
            <Rows3 size={13} className="menu-item-icon" aria-hidden />
            <span className="menu-item-name">Row height</span>
            <PanelSelect
              className="row-height-select"
              data-testid="row-height-select"
              aria-label="Row height"
              value={rowHeight}
              menuRef={rowHeightMenuRef}
              onChange={(e) => changeRowHeight(Number(e.target.value))}
            >
              {ROW_HEIGHTS.map((h) => (
                <option key={h.value} value={h.value}>{h.label}</option>
              ))}
            </PanelSelect>
          </div>
          <button
            type="button"
            ref={savedViewsTriggerRef}
            className="menu-item"
            data-testid="open-saved-views-submenu"
            aria-haspopup="menu"
            aria-expanded={savedViewsMenuOpen}
            onPointerEnter={openSavedViewsSubmenu}
            onFocus={() => {
              if (suppressNextSavedViewsFocusOpenRef.current) {
                suppressNextSavedViewsFocusOpenRef.current = false;
                return;
              }
              openSavedViewsSubmenu();
            }}
            onKeyDown={(event) => {
              if (event.key === 'ArrowRight' || event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                openSavedViewsSubmenu();
                requestAnimationFrame(() => savedViewsMenuRef.current?.querySelector<HTMLElement>('button, input')?.focus());
              }
            }}
            onClick={openSavedViewsSubmenu}
          >
            <BookOpen size={13} className="menu-item-icon" />
            <span className="menu-item-name">Saved views</span>
          </button>
          <button
            type="button"
            className={`menu-item${provenanceOpen ? ' menu-item-active' : ''}`}
            data-testid="open-provenance-manifest"
            onClick={() => {
              setOverflowOpen(false);
              toggleProvenanceWithDrawers();
            }}
          >
            <ShieldCheck size={13} className="menu-item-icon" />
            <span className="menu-item-name">Provenance</span>
          </button>
          {activeHiddenColumns.length > 0 && (
            <button
              type="button"
              className="menu-item"
              data-testid="toolbar-show-hidden-columns"
              onClick={() => {
                setOverflowOpen(false);
                revealAllHiddenColumns();
              }}
            >
              <Eye size={13} className="menu-item-icon" />
              <span className="menu-item-name">
                {`Show ${activeHiddenColumns.length} hidden column${
                  activeHiddenColumns.length === 1 ? '' : 's'
                }`}
              </span>
            </button>
          )}
        </div>
      )}
      {overflowMenuOpen && savedViewsMenuOpen && (
        <div
          ref={savedViewsMenuRef}
          className="menu-pop saved-views-overflow-menu"
          data-testid="saved-views-overflow-menu"
          role="menu"
          onKeyDown={(event) => {
            if (event.key === 'ArrowLeft') {
              event.preventDefault();
              closeSavedViewsSubmenu();
              suppressNextSavedViewsFocusOpenRef.current = true;
              requestAnimationFrame(() => savedViewsTriggerRef.current?.focus());
            }
          }}
          style={
            savedViewsMenuPos
              ? { position: 'fixed', inset: 'auto', top: savedViewsMenuPos.top, bottom: 'auto', left: savedViewsMenuPos.left, right: 'auto', width: savedViewsMenuPos.width, maxHeight: savedViewsMenuPos.maxHeight, margin: 0 }
              : { position: 'fixed', visibility: 'hidden' }
          }
        >
          {views.length > 8 && (
            <TextInput
              className="saved-views-overflow-search"
              aria-label="Search saved views"
              value={savedViewsQuery}
              onChange={(event) => setSavedViewsQuery(event.target.value)}
            />
          )}
          <div className="saved-views-overflow-list" data-testid="saved-views-overflow-list">
            {filteredViews.length === 0 ? (
              <div className="menu-empty">No matching saved views</div>
            ) : (
              filteredViews.map((view) => (
                <button
                  type="button"
                  className="menu-item"
                  data-testid={`saved-views-overflow-view-${view.id}`}
                  key={view.id}
                  onClick={() => {
                    applySavedView(view);
                    setApplyAnnouncement(`Applied ${view.name}`);
                    setOverflowOpen(false);
                  }}
                >
                  <span className="menu-item-name">{view.name}</span>
                  {activeSavedViewId === view.id && <span className="menu-item-note">Active</span>}
                </button>
              ))
            )}
          </div>
          {canCreateSavedView && (
            <button
              type="button"
              className="menu-item"
              data-testid="saved-views-overflow-new"
              onClick={() => {
                setOverflowOpen(false);
                openNewSavedView();
              }}
            >
              <Plus size={13} className="menu-item-icon" />
              <span className="menu-item-name">New view</span>
            </button>
          )}
          <button
            type="button"
            className="menu-item"
            data-testid="saved-views-overflow-open-panel"
            onClick={() => {
              setOverflowOpen(false);
              openSavedViews();
            }}
          >
            <BookOpen size={13} className="menu-item-icon" />
            <span className="menu-item-name">Open Saved Views</span>
          </button>
        </div>
      )}
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {applyAnnouncement}
      </div>
    </div>
  );
}

export function WorkspaceGridControls() {
  const {
    applyGridSort,
    clearGridFilter,
    clearGridSort,
    effectiveSortColumn,
    sheet,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const activeGridFilter = useSelector(gridView.store, (s) => s.applied.filter);
  const scopeRowIds = useSelector(gridView.store, (s) => s.applied.scopeRowIds ?? null);

  const activeGridFilterValueLabel = useSelector(
    gridView.store,
    (s) => s.applied.filterValueLabel,
  );
  const activeGridSort = useSelector(gridView.store, (s) => s.applied.sort);
  const sortPanelOpen = useSelector(gridView.store, (s) => s.sortPanelOpen);
  const draftSortDirection = useSelector(gridView.store, (s) => s.draft.sortDirection);
  if (!sheet) return null;

  return (
<>
              {sortPanelOpen && (
                <GridSortPanel
                  sheet={sheet}
                  column={effectiveSortColumn}
                  direction={draftSortDirection}
                  onColumnChange={gridView.setDraftSortColumn}
                  onDirectionChange={gridView.setDraftSortDirection}
                  onApply={applyGridSort}
                  onClose={() => gridView.setSortPanelOpen(false)}
                />
              )}

              {(activeGridFilter || activeGridSort || scopeRowIds !== null) && (
                <div className="grid-active-bar">
                  {scopeRowIds !== null && <span className="active-grid-state">{scopeRowIds.length} selected rows <Button type="button" onClick={() => gridView.store.set((state) => ({ ...state, activeSavedViewId: null, applied: { ...state.applied, scopeRowIds: null } }))}>Clear</Button></span>}
                  {activeGridFilter && (
                    <span className="active-grid-state active-grid-filter" data-testid="active-grid-filter">
                      <span className="active-grid-filter-label">
                        Filter: {gridFilterSummaryLabel(activeGridFilter, activeGridFilterValueLabel)}
                      </span>
                      <Button
                        type="button"

                        data-testid="clear-grid-filter"
                        onClick={clearGridFilter}
                      >
                        Clear
                      </Button>
                    </span>
                  )}
                  {activeGridSort && (
                    <span className="active-grid-state" data-testid="active-grid-sort">
                      Sort: {gridSortLabel(activeGridSort)}
                      <Button
                        type="button"

                        data-testid="clear-grid-sort"
                        onClick={clearGridSort}
                      >
                        Clear
                      </Button>
                    </span>
                  )}
                </div>
              )}
  </>
  );
}

export function WorkspaceGridBanners() {
  const {
    activeChildFilter,
    activeLensView,
    activePreviewView,
    clearChildFilter,
    closePreviewView,
    runPreviewForReal,
    exitLensView,
    lensOpenError,
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

              {lensOpenError && !activeLensView && (
                <div className="lens-view-banner lens-view-error" data-testid="lens-view-open-error" role="alert">
                  {lensOpenError}
                </div>
              )}

              {activeLensView && (
                <div className="lens-view-banner" data-testid="lens-view-banner">
                  <Boxes size={13} aria-hidden />
                  <span>
                    Lens view: <strong>{activeLensView.name}</strong> ·{' '}
                    {activeLensView.total > activeLensView.rowIds.length ? (
                      <>
                        showing {activeLensView.rowIds.length.toLocaleString()} of{' '}
                        {activeLensView.total.toLocaleString()} ranked rows
                      </>
                    ) : (
                      <>
                        {activeLensView.rowIds.length.toLocaleString()} ranked{' '}
                        {activeLensView.rowIds.length === 1 ? 'row' : 'rows'}
                      </>
                    )}
                  </span>
                  <span className="lens-view-score-hint" data-testid="lens-view-score-columns">
                    showing distance · score columns
                  </span>
                  <Button
                    type="button"

                    data-testid="lens-view-exit"
                    onClick={exitLensView}
                  >
                    Exit lens view
                  </Button>
                </div>
              )}

              {activePreviewView && <ActionPreviewBanner view={activePreviewView}
                onRun={runPreviewForReal} onClose={closePreviewView} />}
  </>
  );
}
