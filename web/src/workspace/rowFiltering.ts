import type {
  ColumnDef,
  GridFilterOperator,
  GridFilterSpec,
  GridFilterValue,
  GridSortSpec,
  ViewSelection,
} from '../api/types';
import { facetBehaviorFor } from '../grid/typeRegistry';
import { gridFilterSummaryLabel } from './gridColumnState';

/** `all` searches every text column through the selection's full-text search;
 *  a column name searches that one column with a `contains` condition. */
export type SearchScope = 'all' | string;

export interface RowSearch {
  text: string;
  scope: SearchScope;
}

/** The sheet's row-shaping state, read from the grid view store's applied
 *  slice. Chips, the Filter badge and the row count all derive from it. */
export interface RowFilterState {
  selection: ViewSelection | null;
  filter: GridFilterSpec | null;
  filterValueLabel: string | null;
  sort: GridSortSpec | null;
  scopeRowIds: number[] | null;
}

type ColumnCondition = Partial<Record<GridFilterOperator, GridFilterValue>>;

export type ChipTarget =
  | { kind: 'search' }
  | { kind: 'facet'; column: string }
  | { kind: 'rows' }
  | { kind: 'similarity' }
  | { kind: 'sort' }
  | { kind: 'limit' };

export interface RowFilterChip {
  key: string;
  /** Filter chips hide rows by value; shaping chips (sort, limit) do not. */
  tone: 'filter' | 'shape';
  kind: string;
  value: string;
  target: ChipTarget;
}

const CHIP_VALUE_LIMIT = 24;

/** Markdown documents are searched, not faceted. */
function isLongFormText(column: ColumnDef): boolean {
  return column.type === 'text' && column.format === 'markdown';
}

export function isFacetColumn(column: ColumnDef): boolean {
  if (isLongFormText(column)) return false;
  const behavior = facetBehaviorFor(column.type);
  return behavior?.kind === 'range'
    || behavior?.kind === 'collection'
    || (behavior?.kind === 'categorical' && behavior.operator === 'eq');
}

/** Facet cards in the order the sidebar lists them: preferred types first. */
export function facetColumnsOf(columns: ColumnDef[]): ColumnDef[] {
  return columns
    .filter(isFacetColumn)
    .sort((left, right) =>
      Number(facetBehaviorFor(right.type)?.preferred === true)
      - Number(facetBehaviorFor(left.type)?.preferred === true));
}

/** Columns the search scope can name. */
export function searchableColumnsOf(columns: ColumnDef[]): ColumnDef[] {
  return columns.filter((column) => column.type === 'text');
}

function truncateChipValue(value: string): string {
  const characters = Array.from(value);
  return characters.length <= CHIP_VALUE_LIMIT
    ? value
    : `${characters.slice(0, CHIP_VALUE_LIMIT - 1).join('')}…`;
}

/** The facet part of a column condition: everything but the search scope's `contains`. */
export function facetCondition(condition: ColumnCondition | undefined): ColumnCondition | null {
  if (!condition) return null;
  const rest: ColumnCondition = { ...condition };
  delete rest.contains;
  return Object.keys(rest).length > 0 ? rest : null;
}

export function searchOf(state: Pick<RowFilterState, 'selection' | 'filter'>): RowSearch {
  if (state.selection?.search) return { text: state.selection.search.text, scope: 'all' };
  for (const [column, condition] of Object.entries(state.filter ?? {})) {
    if (typeof condition.contains === 'string' && condition.contains !== '') {
      return { text: condition.contains, scope: column };
    }
  }
  return { text: '', scope: 'all' };
}

function filterWithoutSearch(filter: GridFilterSpec | null): GridFilterSpec | null {
  const next: GridFilterSpec = {};
  for (const [column, condition] of Object.entries(filter ?? {})) {
    const facet = facetCondition(condition);
    if (facet) next[column] = facet;
  }
  return Object.keys(next).length > 0 ? next : null;
}

export function withColumnFacet(
  filter: GridFilterSpec | null,
  column: string,
  facet: ColumnCondition | null,
): GridFilterSpec | null {
  const next: GridFilterSpec = { ...(filter ?? {}) };
  const search = next[column]?.contains;
  const merged: ColumnCondition = {
    ...(facet ?? {}),
    ...(typeof search === 'string' ? { contains: search } : {}),
  };
  if (Object.keys(merged).length > 0) next[column] = merged;
  else delete next[column];
  return Object.keys(next).length > 0 ? next : null;
}

/** The selection a facet counts within: every other condition, with this
 *  column's own facet, the limit and the row order left out. */
export function selectionWithoutFacet(selection: ViewSelection, columnId: number): ViewSelection {
  return {
    ...selection,
    filters: selection.filters.flatMap((item) => {
      if (item.column_id !== columnId) return [item];
      const search = item.condition.contains;
      return typeof search === 'string' ? [{ ...item, condition: { contains: search } }] : [];
    }),
    order: [],
    limit: null,
  };
}

/** Search moves between the selection's full-text search and one column's
 *  `contains` condition; the previous home is always cleared. */
export function withSearch(
  state: Pick<RowFilterState, 'selection' | 'filter'>,
  search: RowSearch,
): { selectionSearch: ViewSelection['search']; filter: GridFilterSpec | null } {
  const text = search.text;
  const active = text.trim() !== '';
  let filter = filterWithoutSearch(state.filter);
  if (active && search.scope !== 'all') {
    filter = { ...(filter ?? {}), [search.scope]: { ...(filter?.[search.scope] ?? {}), contains: text } };
  }
  return {
    selectionSearch: active && search.scope === 'all' ? { text } : undefined,
    filter,
  };
}

function facetChipValue(column: string, condition: ColumnCondition, valueLabel: string | null): string {
  const values = condition.in;
  if (Array.isArray(values) && Object.keys(condition).length === 1) {
    if (values.length === 1) return truncateChipValue(String(values[0]));
    return `${values.length.toLocaleString()} selected`;
  }
  if (typeof condition.eq === 'string' && Object.keys(condition).length === 1) {
    return truncateChipValue(condition.eq);
  }
  const range = condition.between;
  if (range && typeof range === 'object' && !Array.isArray(range) && 'start' in range && 'end' in range) {
    return `${range.start}–${range.end}`;
  }
  const label = gridFilterSummaryLabel({ [column]: condition }, valueLabel);
  return label.startsWith(`${column} `) ? label.slice(column.length + 1) : label;
}

export function sortOf(state: Pick<RowFilterState, 'selection' | 'sort'>):
  | { kind: 'column'; column: string; dir: 'asc' | 'desc' }
  | { kind: 'score'; score: 'search' | 'similarity' }
  | null {
  const column = state.sort?.[0];
  if (column) return { kind: 'column', column: column.column, dir: column.dir };
  const score = state.selection?.order.find((item) => 'score' in item);
  return score && 'score' in score ? { kind: 'score', score: score.score } : null;
}

export function deriveRowFilterChips(state: RowFilterState): RowFilterChip[] {
  const chips: RowFilterChip[] = [];
  const search = searchOf(state);
  if (search.text.trim()) {
    chips.push({
      key: 'search',
      tone: 'filter',
      kind: 'Search',
      value: `“${truncateChipValue(search.text)}”${search.scope === 'all' ? '' : ` in ${search.scope}`}`,
      target: { kind: 'search' },
    });
  }
  if (state.scopeRowIds !== null) {
    chips.push({
      key: 'rows',
      tone: 'filter',
      kind: 'Rows',
      value: `${state.scopeRowIds.length.toLocaleString()} selected`,
      target: { kind: 'rows' },
    });
  }
  if (state.selection?.similarity) {
    chips.push({ key: 'similarity', tone: 'filter', kind: 'Similar', value: 'rows', target: { kind: 'similarity' } });
  }
  for (const [column, condition] of Object.entries(state.filter ?? {})) {
    const facet = facetCondition(condition);
    if (!facet) continue;
    chips.push({
      key: `facet:${column}`,
      tone: 'filter',
      kind: column,
      value: facetChipValue(column, facet, state.filterValueLabel),
      target: { kind: 'facet', column },
    });
  }
  const sort = sortOf(state);
  if (sort) {
    chips.push({
      key: 'sort',
      tone: 'shape',
      kind: 'Sorted by',
      value: sort.kind === 'column'
        ? `${sort.column} ${sort.dir === 'asc' ? '↑' : '↓'}`
        : `${sort.score} score`,
      target: { kind: 'sort' },
    });
  }
  const limit = state.selection?.limit;
  if (limit != null) {
    chips.push({
      key: 'limit',
      tone: 'shape',
      kind: 'Showing',
      value: `first ${limit.toLocaleString()}`,
      target: { kind: 'limit' },
    });
  }
  return chips;
}

export function chipSummary(chips: RowFilterChip[]): string {
  return chips.length > 0
    ? chips.map((chip) => `${chip.kind} ${chip.value}`).join(' · ')
    : 'No filters';
}

/** `6 rows · 5 columns`, or `3 of 6 rows · 5 columns` while rows are hidden. */
export function sheetCountLabel(shownRows: number, totalRows: number, columns: number): string {
  const rows = shownRows === totalRows
    ? `${totalRows.toLocaleString()} ${totalRows === 1 ? 'row' : 'rows'}`
    : `${shownRows.toLocaleString()} of ${totalRows.toLocaleString()} ${totalRows === 1 ? 'row' : 'rows'}`;
  return `${rows} · ${columns.toLocaleString()} ${columns === 1 ? 'column' : 'columns'}`;
}

/** The note under the last grid row while a limit hides matching rows. */
export function limitNote(limit: number | null | undefined, matchingRows: number | null): { note: string; showAll: string } | null {
  if (limit == null || matchingRows === null || matchingRows <= limit) return null;
  return {
    note: `Showing first ${limit.toLocaleString()} of ${matchingRows.toLocaleString()} matching rows.`,
    showAll: `Show all ${matchingRows.toLocaleString()}`,
  };
}
