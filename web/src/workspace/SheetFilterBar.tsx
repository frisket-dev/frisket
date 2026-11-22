import type { GridFilterSpec, GridSortSpec, ViewSelection } from '../api/open';
import { Button } from '../components/Button';
import { gridFilterSummaryLabel, gridSortLabel } from './gridColumnState';

export interface SheetFilterBarProps {
  currentSelection: ViewSelection | null;
  onSelectionChange(selection: ViewSelection): void;
  activeGridFilter: GridFilterSpec | null;
  activeGridFilterValueLabel: string | null;
  activeGridSort: GridSortSpec | null;
  scopeRowIds: number[] | null;
  onClearGridFilter(): void;
  onClearGridSort(): void;
  onClearScopeRowIds(): void;
}

export function SheetFilterBar({
  currentSelection,
  onSelectionChange,
  activeGridFilter,
  activeGridFilterValueLabel,
  activeGridSort,
  scopeRowIds,
  onClearGridFilter,
  onClearGridSort,
  onClearScopeRowIds,
}: SheetFilterBarProps) {
  const hasActiveChips = Boolean(
    activeGridFilter ||
    activeGridSort ||
    scopeRowIds !== null ||
    currentSelection?.search ||
    currentSelection?.similarity ||
    currentSelection?.limit != null,
  );

  if (!hasActiveChips) return null;

  return (
    <div className="grid-active-bar" data-testid="grid-active-bar">
      {scopeRowIds !== null && (
        <span className="active-grid-state" data-testid="active-grid-scope">
          {scopeRowIds.length} selected rows{' '}
          <Button type="button" onClick={onClearScopeRowIds}>
            Clear
          </Button>
        </span>
      )}
      {currentSelection?.search && (
        <span className="active-grid-state" data-testid="active-grid-search">
          Search: “{currentSelection.search.text}”
          <Button
            type="button"
            data-testid="clear-grid-search"
            onClick={() =>
              onSelectionChange({
                ...currentSelection,
                search: undefined,
                order: currentSelection.order.filter(
                  (item) => !('score' in item && item.score === 'search'),
                ),
              })
            }
          >
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
      {currentSelection?.limit != null && (
        <span className="active-grid-state" data-testid="active-grid-limit">
          Limit: {currentSelection.limit.toLocaleString()}{' '}
          {currentSelection.limit === 1 ? 'row' : 'rows'}
          <Button
            type="button"
            data-testid="clear-grid-limit"
            onClick={() =>
              onSelectionChange({
                ...currentSelection,
                limit: null,
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
  );
}
