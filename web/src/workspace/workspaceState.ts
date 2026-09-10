import {
  type CopilotActionSpec,
  type ColumnDef,
  type GridFilterOperator,
} from '../api/open';

export interface SelectedGridRows {
  sheetId: string;
  rowIds: string[];
  rowIndexes: number[];
}

export interface ChildFilter {
  sheetId: string;
  parentSheetName: string;
  parentRowId: string;
  parentRowIndex: number | null;
  count: number;
}

export interface HeaderMenuState {
  column: ColumnDef;
  columnIndex: number;
  bounds: { x: number; y: number; width: number; height: number };
}

// PreviewGridView was the same kind of neighbor until WEB-05-B1 moved it —
// and its own owner — into state/previewViewStore.ts. The saved-lens grid-view
// neighbor followed the same pattern in WEB-05-C.
export interface ProposalInspectState {
  seq: number;
  title: string;
  spec: CopilotActionSpec;
}

/** Global row heights, Google-Sheets-style: resizing applies to every row. */
export const ROW_HEIGHTS = [
  { label: 'Compact', value: 26 },
  { label: 'Default', value: 34 },
  { label: 'Roomy', value: 48 },
  { label: 'Tall', value: 68 },
  { label: 'Extra', value: 96 },
] as const;

export const WRAP_ROW_HEIGHT = 68; // wrapping needs room; auto-bump short rows

export const emptySelectedRows = (sheetId = ''): SelectedGridRows => ({
  sheetId,
  rowIds: [],
  rowIndexes: [],
});

/** The operators the MANUAL filter editor offers. Deliberately a closed
 *  scalar set: `bbox` (map-set), `failed` (triage-set) and `entity_eq`
 *  (Mentions-panel-set) are programmatic filters whose values are a box, a
 *  taxonomy bucket, and a `{type, text|fingerprint}` payload — none of which
 *  the friendly facet's scalar options can author or edit. Because
 *  filterOperatorForColumnType coerces anything missing from this list to
 *  'eq', omission here is what keeps those three out of the editor. */
export function filterOperatorOptions(
  columnType?: ColumnDef['type'],
): Array<{ value: GridFilterOperator; label: string }> {
  if (columnType === 'boolean') {
    return [
      { value: 'eq', label: 'equals' },
      { value: 'neq', label: 'not equals' },
    ];
  }
  if (columnType === 'date') {
    return [
      { value: 'eq', label: 'is on' },
      { value: 'neq', label: 'is not on' },
      { value: 'gte', label: 'on or after' },
      { value: 'lte', label: 'on or before' },
      { value: 'between', label: 'between' },
      { value: 'date_relative', label: 'in the last…' },
      { value: 'date_this_year', label: 'this year' },
      { value: 'date_ytd', label: 'year to date' },
      { value: 'date_year', label: 'year is…' },
      { value: 'date_month', label: 'month is…' },
      { value: 'date_weekday', label: 'day of week is…' },
      { value: 'date_invalid', label: 'is not a valid date' },
    ];
  }
  if (columnType === 'integer' || columnType === 'number') {
    return [
      { value: 'eq', label: 'equals' },
      { value: 'gte', label: 'at least' },
      { value: 'lte', label: 'at most' },
      { value: 'between', label: 'between' },
      { value: 'neq', label: 'not equals' },
    ];
  }
  return [
    { value: 'eq', label: 'equals' },
    { value: 'contains', label: 'contains' },
    { value: 'neq', label: 'not equals' },
  ];
}

export function filterOperatorForColumnType(
  columnType: ColumnDef['type'] | undefined,
  operator: GridFilterOperator,
): GridFilterOperator {
  return filterOperatorOptions(columnType).some((item) => item.value === operator)
    ? operator
    : 'eq';
}
