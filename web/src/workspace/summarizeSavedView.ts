import type { SavedView } from '../api/open';
import {
  gridFilterLabel,
  gridSortLabel,
  normalizeGridFilterSpec,
  normalizeGridSortSpec,
} from './gridColumnState';

export interface SavedViewSummary {
  /** The filter's display text, including the explicit unfiltered state. */
  filterSummary: string;
  /** null when sorting is not part of the saved presentation. */
  sortSummary: string | null;
  /** null when the view has no custom displayed-column list. */
  columnCount: number | null;
  hasNoEffectiveFilter: boolean;
  /** The compact summary used wherever a Saved View is displayed. */
  summary: string;
}

function readableFilterSummary(filter: NonNullable<ReturnType<typeof normalizeGridFilterSpec>>): string {
  // The grid's normalizer deliberately preserves wire operators (such as
  // `eq`). Saved View descriptions are user-facing prose.
  return gridFilterLabel(filter).replace(/\seq\s/g, ' equals ');
}

function readableSortSummary(sort: NonNullable<ReturnType<typeof normalizeGridSortSpec>>): string {
  const label = gridSortLabel(sort);
  if (label.endsWith(' asc')) return `${label.slice(0, -4)} ascending`;
  if (label.endsWith(' desc')) return `${label.slice(0, -5)} descending`;
  return label;
}

/**
 * Describes only a Saved View's persisted presentation/query state. Consumers
 * may carry these fields as display metadata, but never use them as Watch
 * execution authority.
 */
export function summarizeSavedView(view: SavedView): SavedViewSummary {
  const filter = normalizeGridFilterSpec(view.spec.filter);
  const sort = normalizeGridSortSpec(view.spec.sort);
  const columns = Array.isArray(view.spec.columns)
    ? view.spec.columns.filter((column): column is string => typeof column === 'string')
    : null;
  const filterSummary = filter ? readableFilterSummary(filter) : 'No filters';
  const sortSummary = sort ? readableSortSummary(sort) : null;
  const columnCount = columns === null ? null : columns.length;

  return {
    filterSummary,
    sortSummary,
    columnCount,
    hasNoEffectiveFilter: filter === null,
    summary: [
      filterSummary,
      sortSummary,
      columnCount === null ? null : `${columnCount} columns`,
    ].filter((part): part is string => part !== null).join(' · '),
  };
}
