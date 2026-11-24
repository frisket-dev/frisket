import type { View, ViewDefinitionInput } from '../api/open';

interface ViewSummary {
  filterSummary: string;
  sortSummary: string | null;
  columnCount: number | null;
  hasNoEffectiveFilter: boolean;
  summary: string;
}

function summarizeViewDefinition(
  spec: ViewDefinitionInput,
  unavailableReason: string | null = null,
): ViewSummary {
  const { selection, presentation } = spec;
  const criteria = [
    selection.search ? `Search “${selection.search.text}”` : null,
    selection.similarity ? 'Similarity' : null,
    selection.filters.length ? `${selection.filters.length} filter${selection.filters.length === 1 ? '' : 's'}` : null,
    selection.scope.kind === 'sheet' && selection.scope.row_ids ? `${selection.scope.row_ids.length} selected rows` : null,
  ].filter(Boolean);
  const filterSummary = criteria.join(' · ') || 'No filters';
  const sortSummary = selection.order.length ? `${selection.order.length} sort${selection.order.length === 1 ? '' : 's'}` : null;
  const columnCount = presentation.columns?.length ?? null;
  return {
    filterSummary,
    sortSummary,
    columnCount,
    hasNoEffectiveFilter: criteria.length === 0,
    summary: unavailableReason ?? [
      filterSummary,
      sortSummary,
      selection.limit ? `First ${selection.limit.toLocaleString()}` : null,
      columnCount === null ? null : `${columnCount} columns`,
    ].filter(Boolean).join(' · '),
  };
}

export function summarizeView(view: View): ViewSummary {
  if (!view.spec) {
    return {
      filterSummary: 'Unavailable',
      sortSummary: null,
      columnCount: null,
      hasNoEffectiveFilter: false,
      summary: view.unavailable_reason ?? 'Unavailable view',
    };
  }
  return summarizeViewDefinition(view.spec, view.unavailable_reason);
}
