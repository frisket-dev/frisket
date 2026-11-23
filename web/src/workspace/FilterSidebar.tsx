import './FilterSidebar.css';
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { ArrowDown, ArrowUp, Search, X } from 'lucide-react';
import { useCurrentViewSelection } from '../bind/useCurrentViewSelection';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useSelector } from '../bind/useSelector';
import { PanelSelect, type PanelSelectOption } from '../components/PanelSelect';
import { SegmentedToggle } from '../components/PanelPrimitives';
import { TextInput } from '../components/FormControls';
import { FriendlyFiltersWorkbenchPanel } from '../workbench/contributions';
import type { FilterSidebarTab } from '../state/gridViewStore';
import type { GridFilterSpec, GridSortDirection, GridSortSpec, SheetMeta, ViewSelection } from '../api/types';
import { deriveRowFilterChips, searchOf, searchScopeColumnsOf, sortOf, type RowSearch } from './rowFiltering';
import { SheetViewsTab } from './SheetViewsTab';
import { useCurrentSheet, useMainViewModel, useWorkbenchHostContext } from './WorkspaceContext';

const SEARCH_DEBOUNCE_MS = 180;
const DEFAULT_LIMIT = 100;
export const FILTER_SIDEBAR_ID = 'sheet-filter-sidebar';
export const FILTER_TOGGLE_TEST_ID = 'filter-sidebar-toggle';

function focusFilterToggle() {
  document.querySelector<HTMLElement>(`[data-testid="${FILTER_TOGGLE_TEST_ID}"]`)?.focus();
}

export interface FilterRowsControlsProps {
  sheet: SheetMeta;
  selection: ViewSelection | null;
  filter: GridFilterSpec | null;
  sort: GridSortSpec | null;
  /** Increments when a caller asks the search box to take focus. */
  focusSearch: number;
  /** The scope the next search text applies to while no search is active. */
  draftScope: string;
  /** Columns the facet cap leaves to search; the scope can name them. */
  searchOnlyColumns: ReadonlySet<string>;
  onSearch(search: RowSearch): void;
  onDraftScope(scope: string): void;
  onLimit(limit: number | null): void;
  onOrder(order: { column: string; dir: GridSortDirection } | null): void;
}

/** Search, sort and limit: every row-level control lives here, not on the grid. */
export function FilterRowsControls({
  sheet,
  selection,
  filter,
  sort,
  focusSearch,
  draftScope,
  searchOnlyColumns,
  onSearch,
  onDraftScope,
  onLimit,
  onOrder,
}: FilterRowsControlsProps) {
  const search = searchOf({ selection, filter });
  const scope = search.text ? search.scope : draftScope;
  const order = sortOf({ sort });
  const limit = selection?.limit ?? null;

  const [draft, setDraft] = useState(search.text);
  const [lastExternal, setLastExternal] = useState(search.text);
  const [limitDraft, setLimitDraft] = useState(String(limit ?? DEFAULT_LIMIT));
  const [lastLimit, setLastLimit] = useState(limit);
  const timer = useRef<number | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  if (search.text !== lastExternal) {
    setLastExternal(search.text);
    setDraft(search.text);
  }
  if (limit !== lastLimit) {
    setLastLimit(limit);
    if (limit !== null) setLimitDraft(String(limit));
  }

  const pending = useRef<RowSearch | null>(null);
  const onSearchRef = useRef(onSearch);
  useEffect(() => {
    onSearchRef.current = onSearch;
  }, [onSearch]);
  const cancelPending = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
    pending.current = null;
  }, []);
  // A search still waiting on its debounce is committed, not dropped, when the
  // box loses focus or the controls unmount (a tab switch). The owner rejects
  // it if the sheet has changed meanwhile.
  const flushPending = useCallback(() => {
    const search = pending.current;
    cancelPending();
    if (search) onSearchRef.current(search);
  }, [cancelPending]);
  useEffect(() => flushPending, [flushPending]);
  useEffect(() => {
    if (focusSearch > 0) searchRef.current?.focus();
  }, [focusSearch]);

  const textColumns = searchScopeColumnsOf(sheet.columns, { selection, filter }, searchOnlyColumns);
  const commit = (text: string, nextScope = scope) => {
    cancelPending();
    onSearch({ text, scope: nextScope });
  };
  const onSearchChange = (next: string) => {
    setDraft(next);
    cancelPending();
    if (next.trim() === '') {
      commit('');
      return;
    }
    pending.current = { text: next, scope };
    timer.current = window.setTimeout(flushPending, SEARCH_DEBOUNCE_MS);
  };

  const sortValue = order === null ? '' : `column:${order.column}`;
  const sortOptions: PanelSelectOption[] = [
    { value: '', label: 'Row order' },
    ...sheet.columns.map((column) => ({ value: `column:${column.name}`, label: column.name })),
  ];
  const direction = order ? order.dir : 'asc';
  const applyLimitDraft = (raw: string) => {
    const parsed = Number(raw);
    if (Number.isInteger(parsed) && parsed > 0) onLimit(parsed);
  };

  return (
    <section className="filter-rows" data-testid="sheet-filter-controls" aria-label="Rows">
      <span className="filter-section-label">Rows</span>
      <div className="filter-search" role="search">
        <Search size={14} className="filter-search-icon" aria-hidden />
        <input
          ref={searchRef}
          type="text"
          className="filter-search-input"
          data-testid="sheet-filter-search"
          aria-label="Search text"
          placeholder="Search"
          data-active={draft ? 'true' : undefined}
          value={draft}
          onChange={(event) => onSearchChange(event.currentTarget.value)}
          onBlur={flushPending}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              commit(draft);
            }
          }}
        />
        {draft && (
          <button
            type="button"
            className="filter-search-clear"
            data-testid="sheet-filter-search-clear"
            aria-label="Clear search"
            onClick={() => {
              setDraft('');
              commit('');
              searchRef.current?.focus();
            }}
          >
            <X size={12} aria-hidden />
          </button>
        )}
      </div>
      <label className="filter-row filter-scope">
        <span>in</span>
        <PanelSelect
          testId="sheet-filter-scope"
          ariaLabel="Search in"
          value={scope}
          options={[
            { value: 'all', label: 'All text columns' },
            ...textColumns.map((column) => ({ value: column.name, label: column.name })),
          ]}
          onValueChange={(next) => {
            onDraftScope(next);
            if (draft.trim()) commit(draft, next);
          }}
        />
      </label>
      <div className="filter-row">
        <span className="filter-row-label">Sort</span>
        <PanelSelect
          testId="sheet-filter-sort"
          ariaLabel="Sort by"
          value={sortValue}
          options={sortOptions}
          onValueChange={(next) => {
            if (next === '') onOrder(null);
            else onOrder({ column: next.slice('column:'.length), dir: direction });
          }}
        />
        <div className="filter-direction" data-inactive={order ? undefined : 'true'}>
          <SegmentedToggle
            size="compact"
            fullWidth={false}
            ariaLabel="Sort direction"
            testId="sheet-filter-sort-direction"
            disabled={!order}
            value={order ? order.dir : ''}
            options={[
              { value: 'asc', label: 'Asc', icon: ArrowUp },
              { value: 'desc', label: 'Desc', icon: ArrowDown },
            ]}
            onValueChange={(next) => {
              if (order) onOrder({ column: order.column, dir: next === 'desc' ? 'desc' : 'asc' });
            }}
          />
        </div>
      </div>
      <div className="filter-row">
        <span className="filter-row-label">Show</span>
        <SegmentedToggle
          size="compact"
          fullWidth={false}
          ariaLabel="Show"
          testId="sheet-filter-show"
          value={limit === null ? 'all' : 'first'}
          options={[
            { value: 'all', label: 'All rows' },
            { value: 'first', label: 'First' },
          ]}
          onValueChange={(next) => {
            if (next === 'all') onLimit(null);
            else applyLimitDraft(limitDraft);
          }}
        />
        <TextInput
          className="filter-limit-input"
          density="compact"
          fullWidth={false}
          data-testid="sheet-filter-limit"
          aria-label="Number of rows"
          type="number"
          min={1}
          inputMode="numeric"
          data-active={limit !== null ? 'true' : undefined}
          value={limitDraft}
          onFocus={() => {
            if (limit === null) applyLimitDraft(limitDraft);
          }}
          onChange={(event) => {
            setLimitDraft(event.currentTarget.value);
            applyLimitDraft(event.currentTarget.value);
          }}
        />
        <span>rows</span>
      </div>
    </section>
  );
}

interface FilterRowsBindingProps {
  sheet: SheetMeta;
  searchOnlyColumns: ReadonlySet<string>;
  applyRowSearch(search: RowSearch, sheetId: string): void;
  applyRowLimit(limit: number | null): void;
  applyRowOrder(order: { column: string; dir: GridSortDirection } | null): void;
}

/** The Rows controls bound to the grid view store and the grid controller. */
export function FilterRowsBinding({ sheet, searchOnlyColumns, applyRowSearch, applyRowLimit, applyRowOrder }: FilterRowsBindingProps) {
  const gridView = useGridViewHandle();
  const filter = useSelector(gridView.store, (s) => s.applied.filter);
  const sort = useSelector(gridView.store, (s) => s.applied.sort);
  const focusSearch = useSelector(gridView.store, (s) => s.sidebar.focusSearch);
  const draftScope = useSelector(gridView.store, (s) => s.sidebar.searchScope);
  const selection = useCurrentViewSelection(sheet);
  return (
    <FilterRowsControls
      key={sheet.id}
      sheet={sheet}
      selection={selection}
      filter={filter}
      sort={sort}
      focusSearch={focusSearch}
      draftScope={draftScope}
      searchOnlyColumns={searchOnlyColumns}
      onSearch={(search) => applyRowSearch(search, sheet.id)}
      onDraftScope={gridView.setSearchScope}
      onLimit={applyRowLimit}
      onOrder={applyRowOrder}
    />
  );
}

function FilterRowsSection() {
  const { sheet, applyRowSearch, applyRowLimit, applyRowOrder, facetEligibility } = useMainViewModel();
  if (!sheet) return null;
  return (
    <FilterRowsBinding
      sheet={sheet}
      searchOnlyColumns={facetEligibility.searchOnly}
      applyRowSearch={applyRowSearch}
      applyRowLimit={applyRowLimit}
      applyRowOrder={applyRowOrder}
    />
  );
}

function FilterTab() {
  const { sheets } = useCurrentSheet();
  const { sheet, facetEligibility } = useMainViewModel();
  const hostContext = useWorkbenchHostContext();
  const gridView = useGridViewHandle();
  const focusFacet = useSelector(gridView.store, (s) => s.sidebar.focusFacet);
  const selection = useCurrentViewSelection(sheet);
  const ensureFacetEligibility = facetEligibility.ensure;
  useEffect(() => {
    ensureFacetEligibility();
  }, [ensureFacetEligibility]);
  const hiddenFacets = useMemo(
    () => new Set([...facetEligibility.searchOnly, ...facetEligibility.pending]),
    [facetEligibility],
  );
  return (
    <FriendlyFiltersWorkbenchPanel
      host="rightInspector"
      sheets={sheets}
      hostContext={hostContext}
      currentSelection={selection}
      focusFacet={focusFacet}
      rowsSection={<FilterRowsSection />}
      hiddenFacetColumns={hiddenFacets}
    />
  );
}

const TABS: Array<{ id: FilterSidebarTab; label: string }> = [
  { id: 'filter', label: 'Filter' },
  { id: 'views', label: 'Views' },
];

/** The sheet's Filter sidebar: the one place to edit search, sort, limit and
 *  facets, and where views are saved and applied. */
export function FilterSidebar() {
  const { sheet, clearViewSelection } = useMainViewModel();
  const gridView = useGridViewHandle();
  const sidebar = useSelector(gridView.store, (s) => s.sidebar);
  const applied = useSelector(gridView.store, (s) => s.applied);
  const selection = useCurrentViewSelection(sheet);
  const tabRefs = useRef(new Map<FilterSidebarTab, HTMLButtonElement>());
  const wasOpen = useRef(false);

  const close = useCallback(() => {
    gridView.closeSidebar();
    focusFilterToggle();
  }, [gridView]);

  useEffect(() => {
    if (sidebar.open && !wasOpen.current && sidebar.focusFacet === null) {
      const active = document.activeElement;
      if (!(active instanceof HTMLInputElement)) tabRefs.current.get(sidebar.tab)?.focus();
    }
    wasOpen.current = sidebar.open;
  }, [sidebar.open, sidebar.focusFacet, sidebar.tab]);

  if (!sidebar.open || !sheet) return null;
  const hasActive = deriveRowFilterChips({
    selection,
    filter: applied.filter,
    filterValueLabel: applied.filterValueLabel,
    sort: applied.sort,
    scopeRowIds: applied.scopeRowIds ?? null,
  }).length > 0;

  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const next = sidebar.tab === 'filter' ? 'views' : 'filter';
    gridView.setSidebarTab(next);
    tabRefs.current.get(next)?.focus();
  };

  return (
    <aside
      id={FILTER_SIDEBAR_ID}
      className="filter-sidebar"
      data-testid="filter-sidebar"
      aria-label="Filter and views"
      onKeyDown={(event) => {
        // Dialogs and menus opened from the sidebar close themselves on Escape.
        const target = event.target instanceof Element ? event.target : null;
        const nested = target?.closest('dialog, [role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"], [popover]');
        if (event.key === 'Escape' && !event.defaultPrevented && !nested) {
          event.preventDefault();
          close();
        }
      }}
    >
      <div className="filter-sidebar-header">
        <div className="filter-sidebar-tabs" role="tablist" aria-label="Filter sidebar">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              ref={(node) => {
                if (node) tabRefs.current.set(tab.id, node);
                else tabRefs.current.delete(tab.id);
              }}
              type="button"
              role="tab"
              id={`filter-sidebar-tab-${tab.id}`}
              aria-selected={sidebar.tab === tab.id}
              aria-controls={`filter-sidebar-panel-${tab.id}`}
              tabIndex={sidebar.tab === tab.id ? 0 : -1}
              data-testid={`filter-sidebar-tab-${tab.id}`}
              className="filter-sidebar-tab"
              onClick={() => gridView.setSidebarTab(tab.id)}
              onKeyDown={onTabKeyDown}
            >
              {tab.label}
            </button>
          ))}
        </div>
        {sidebar.tab === 'filter' && hasActive && (
          <button
            type="button"
            className="filter-sidebar-clear"
            data-testid="filter-sidebar-clear-all"
            onClick={clearViewSelection}
          >
            Clear all
          </button>
        )}
        <button
          type="button"
          className="icon-btn filter-sidebar-close"
          data-testid="filter-sidebar-close"
          aria-label="Close filter sidebar"
          title="Close"
          onClick={close}
        >
          <X size={14} aria-hidden />
        </button>
      </div>
      <div
        className="filter-sidebar-body"
        role="tabpanel"
        id={`filter-sidebar-panel-${sidebar.tab}`}
        aria-labelledby={`filter-sidebar-tab-${sidebar.tab}`}
      >
        {sidebar.tab === 'filter' ? <FilterTab /> : <SheetViewsTab />}
      </div>
    </aside>
  );
}
