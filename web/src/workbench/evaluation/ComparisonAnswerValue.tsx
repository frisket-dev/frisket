import type { CellValue, ColumnDef, JsonValue, Row } from '../../api/types';
import { FieldValue } from '../../components/rowInspection/FieldValue';
import type { ActionCompareSessionSnapshot } from '../actionCompareSession';

/** Comparison answers retain the ordinary typed Inspect/Review presentation. */
export function ComparisonAnswerValue({ state, rowId, field, value }: {
  state: ActionCompareSessionSnapshot; rowId: number; field: string; value: JsonValue | undefined;
}) {
  const output = state.context.output_columns.find((column) => column.logical_key === field);
  const column: ColumnDef = { id: String(output?.column_id ?? field), name: output?.current_name ?? field,
    type: output?.column_type as ColumnDef['type'] ?? 'text', format: output?.format, semanticType: output?.semantic_type };
  const rendered: CellValue = value == null ? null : typeof value === 'object' ? JSON.stringify(value) : value;
  const row: Row = { id: String(rowId), index: state.context.sample_row_ids.indexOf(rowId),
    cells: { [column.id]: rendered }, provenance: {} };
  return <FieldValue col={column} columns={[column]} row={row} sheetId={String(state.context.sheet.id)} value={rendered} />;
}
