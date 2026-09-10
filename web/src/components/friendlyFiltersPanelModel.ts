// Pure view-model helpers for FriendlyFiltersPanel.tsx,
// extracted so the browse-and-filter logic is unit-testable without a DOM:
// the active-filter readback and the honest "top N of M" value summary.
// No React, no api.

import type { ColumnValuesPreview, GridFilterSpec } from '../api/open';

/** One-line explainer for the panel. */
export const VALUE_FACET_BLURB =
  'Distinct values in one column with their counts. Click a value to filter ' +
  'the grid to matching rows.';

function stringCondition(
  filter: GridFilterSpec | null | undefined,
  columnName: string,
  operator: 'eq' | 'contains',
): string | null {
  const value = filter?.[columnName]?.[operator];
  return typeof value === 'string' ? value : null;
}

/** The exact-match value currently filtering `columnName`, if any. */
export function activeEqValue(
  filter: GridFilterSpec | null | undefined,
  columnName: string,
): string | null {
  return stringCondition(filter, columnName, 'eq');
}

/** Honest "top N of M" line for the value facet: the page never claims more
 *  than the caps/truncation the read-only preview reports.
 *
 *  When a search is active the arithmetic changes: `distinct` and `missing`
 *  are UNFILTERED whole-column facts (the payload carries no filtered total),
 *  so the line reports the matches ON THIS PAGE against the whole-column
 *  distinct count and drops the blank tally, which would otherwise read as a
 *  property of the matches. `truncated` still means "more values match beyond
 *  this page". */
export function valueFacetSummary(preview: ColumnValuesPreview): string {
  const shown = preview.values.length;
  const total = preview.distinct;
  if (preview.search) {
    if (shown === 0) {
      return `No matches in ${total.toLocaleString()} distinct values`;
    }
    return preview.truncated
      ? `First ${shown.toLocaleString()} matches of ${total.toLocaleString()} distinct values`
      : `${shown.toLocaleString()} matching ${shown === 1 ? 'value' : 'values'} of ${total.toLocaleString()} distinct`;
  }
  if (total === 0) return 'No values in this column.';
  const head =
    preview.truncated || shown < total
      ? `Top ${shown.toLocaleString()} of ${total.toLocaleString()} distinct values`
      : `${total.toLocaleString()} distinct ${total === 1 ? 'value' : 'values'}`;
  const blanks =
    preview.missing > 0
      ? ` · ${preview.missing.toLocaleString()} blank ${preview.missing === 1 ? 'cell' : 'cells'}`
      : '';
  return head + blanks;
}
