import { useCallback, useEffect, useRef, useState } from 'react';
import { Search, X } from 'lucide-react';
import type { GridFilterSpec, GridSortSpec, ViewSelection } from '../api/open';
import { Button } from '../components/Button';
import { TextInput } from '../components/FormControls';
import { PanelSelect } from '../components/PanelSelect';
import { gridFilterSummaryLabel, gridSortLabel } from './gridColumnState';

const DEFAULT_SEARCH_DEBOUNCE_MS = 180;

export interface SheetFilterBarProps {
  currentSelection: ViewSelection | null;
  onSelectionChange(selection: ViewSelection): void;
  onClearSelection(): void;
  activeGridFilter: GridFilterSpec | null;
  activeGridFilterValueLabel: string | null;
  activeGridSort: GridSortSpec | null;
  scopeRowIds: number[] | null;
  activeViewId: number | null;
  onClearGridFilter(): void;
  onClearGridSort(): void;
  onClearScopeRowIds(): void;
  searchDebounceMs?: number;
}

export function SheetFilterBar({
  currentSelection,
  onSelectionChange,
  onClearSelection,
  activeGridFilter,
  activeGridFilterValueLabel,
  activeGridSort,
  scopeRowIds,
  activeViewId,
  onClearGridFilter,
  onClearGridSort,
  onClearScopeRowIds,
  searchDebounceMs = DEFAULT_SEARCH_DEBOUNCE_MS,
}: SheetFilterBarProps) {
  const externalSearch = currentSelection?.search?.text ?? '';
  const [searchDraft, setSearchDraft] = useState(externalSearch);
  const lastExternalSearchRef = useRef(externalSearch);
  const selectionRef = useRef(currentSelection);
  const onSelectionChangeRef = useRef(onSelectionChange);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const debounceTimerRef = useRef<number | null>(null);

  useEffect(() => {
    selectionRef.current = currentSelection;
  }, [currentSelection]);

  useEffect(() => {
    onSelectionChangeRef.current = onSelectionChange;
  }, [onSelectionChange]);

  const cancelPendingSearch = useCallback(() => {
    if (debounceTimerRef.current !== null) {
      window.clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }
  }, []);

  useEffect(() => () => cancelPendingSearch(), [cancelPendingSearch]);

  useEffect(() => {
    if (externalSearch === lastExternalSearchRef.current) return;
    lastExternalSearchRef.current = externalSearch;
    cancelPendingSearch();
    setSearchDraft(externalSearch);
  }, [cancelPendingSearch, externalSearch]);

  const commitSearch = useCallback((rawText: string) => {
    cancelPendingSearch();
    const selection = selectionRef.current;
    if (!selection) return;
    const trimmed = rawText.trim();
    const nextSearch = trimmed ? { text: rawText } : undefined;
    if ((selection.search?.text ?? '') === (nextSearch?.text ?? '')) return;
    lastExternalSearchRef.current = nextSearch?.text ?? '';
    onSelectionChangeRef.current({
      ...selection,
      search: nextSearch,
      order: !trimmed
        ? selection.order.filter((item) => !('score' in item && item.score === 'search'))
        : selection.order,
    });
  }, [cancelPendingSearch]);

  const handleSearchChange = (next: string) => {
    setSearchDraft(next);
    cancelPendingSearch();
    if (next.trim() === '' || searchDebounceMs <= 0) {
      commitSearch(next);
      return;
    }
    debounceTimerRef.current = window.setTimeout(() => {
      debounceTimerRef.current = null;
      commitSearch(next);
    }, searchDebounceMs);
  };

  const handleClearSearch = () => {
    setSearchDraft('');
    commitSearch('');
    searchInputRef.current?.focus();
  };

  const handleShowAllRows = () => {
    cancelPendingSearch();
    lastExternalSearchRef.current = '';
    setSearchDraft('');
    onClearSelection();
  };

  const hasScoreSort = Boolean(currentSelection?.search || currentSelection?.similarity);
  const scoreOrderValue = currentSelection
    ? currentSelection.order[0] && 'score' in currentSelection.order[0]
      ? currentSelection.order[0].score
      : currentSelection.order.length
        ? 'column'
        : 'rows'
    : 'rows';
  const hasActiveChips = Boolean(
    activeGridFilter || activeGridSort || scopeRowIds !== null || currentSelection?.similarity,
  );
  const hasActiveSelection = Boolean(
    hasActiveChips ||
    currentSelection?.search ||
    currentSelection?.limit != null ||
    activeViewId !== null,
  );

  return (
    <>
      {currentSelection && (
        <div className="sheet-filter-bar" data-testid="sheet-filter-bar" role="search" aria-label="Filter sheet">
          <div className="sheet-filter-search">
            <Search size={13} className="sheet-filter-search-icon" aria-hidden />
            <TextInput
              ref={searchInputRef}
              className="sheet-filter-search-input"
              density="compact"
              data-testid="sheet-search-input"
              aria-label="Search sheet content"
              placeholder="Search text…"
              value={searchDraft}
              onChange={(event) => handleSearchChange(event.currentTarget.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  commitSearch(searchDraft);
                } else if (event.key === 'Escape' && searchDraft) {
                  event.preventDefault();
                  handleClearSearch();
                }
              }}
            />
            {searchDraft && (
              <button
                type="button"
                className="sheet-filter-search-clear"
                data-testid="clear-sheet-search"
                aria-label="Clear search"
                onClick={handleClearSearch}
              >
                <X size={12} aria-hidden />
              </button>
            )}
          </div>
          {hasScoreSort && (
            <label className="sheet-filter-field">
              <span className="sheet-filter-field-label">Sort</span>
              <PanelSelect
                className="sheet-filter-sort-select"
                data-testid="sheet-score-order"
                aria-label="Sort results"
                value={scoreOrderValue}
                onChange={(event) =>
                  onSelectionChange({
                    ...currentSelection,
                    order:
                      event.currentTarget.value === 'rows'
                        ? []
                        : [{ score: event.currentTarget.value as 'search' | 'similarity', dir: 'desc' }],
                  })
                }
              >
                {currentSelection.order.some((item) => 'column_id' in item) && (
                  <option value="column" disabled>Current column sort</option>
                )}
                <option value="rows">Row order</option>
                {currentSelection.search && (
                  <option value="search">Search score, highest first</option>
                )}
                {currentSelection.similarity && (
                  <option value="similarity">Similarity score, highest first</option>
                )}
              </PanelSelect>
            </label>
          )}
          <label className="sheet-filter-field">
            <span className="sheet-filter-field-label">Limit</span>
            <TextInput
              className="sheet-filter-limit-input"
              density="compact"
              fullWidth={false}
              data-testid="sheet-limit-input"
              aria-label="Limit results"
              type="number"
              min={1}
              inputMode="numeric"
              placeholder="All"
              value={currentSelection.limit ?? ''}
              onChange={(event) => {
                const raw = event.currentTarget.value;
                const limit = raw === '' ? null : Number(raw);
                if (limit === null || (Number.isInteger(limit) && limit > 0)) {
                  onSelectionChange({ ...currentSelection, limit });
                }
              }}
            />
          </label>
          {hasActiveSelection && (
            <Button
              type="button"
              className="sheet-filter-reset"
              data-testid="show-all-rows"
              onClick={handleShowAllRows}
            >
              Show all rows
            </Button>
          )}
        </div>
      )}

      {hasActiveChips && (
        <div className="grid-active-bar">
          {scopeRowIds !== null && (
            <span className="active-grid-state">
              {scopeRowIds.length} selected rows{' '}
              <Button type="button" onClick={onClearScopeRowIds}>
                Clear
              </Button>
            </span>
          )}
          {currentSelection?.similarity && (
            <span className="active-grid-state" data-testid="active-grid-similarity">
              Similarity filter
              <Button
                type="button"
                data-testid="clear-grid-similarity"
                onClick={() =>
                  onSelectionChange({
                    ...currentSelection,
                    similarity: undefined,
                    order: currentSelection.order.filter(
                      (item) => !('score' in item && item.score === 'similarity'),
                    ),
                  })
                }
              >
                Clear
              </Button>
            </span>
          )}
          {activeGridFilter && (
            <span className="active-grid-state active-grid-filter" data-testid="active-grid-filter">
              <span className="active-grid-filter-label">
                Filter: {gridFilterSummaryLabel(activeGridFilter, activeGridFilterValueLabel)}
              </span>
              <Button
                type="button"
                data-testid="clear-grid-filter"
                onClick={onClearGridFilter}
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
                onClick={onClearGridSort}
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
