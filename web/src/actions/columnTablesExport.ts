import { freshActionRequestKey, type RegisteredActionRequest } from '../api/types';
import type { ExportColumnTablesParams } from '../generated/actionTypes';

/** Values authored by the export modal and PDF-table pickers. The browser
 * always writes a downloadable project artifact; other destinations remain
 * part of the typed API rather than a dataset export target. */
export interface ColumnTablesExportValue {
  sheetId: string;
  columnId: string;
  groupBy?: string;
  excludeColumns?: string[];
}

function sourceId(value: string, label: string): number {
  const id = Number(value);
  if (!Number.isSafeInteger(id) || id <= 0) throw new Error(`Choose a valid ${label} to export.`);
  return id;
}

export function columnTablesExportRequest(value: ColumnTablesExportValue): RegisteredActionRequest {
  const groupBy = value.groupBy?.trim();
  const excludeColumns = (value.excludeColumns ?? []).map((name) => name.trim()).filter(Boolean);
  const params = {
    sheet_id: sourceId(value.sheetId, 'sheet'),
    column_id: sourceId(value.columnId, 'column'),
    destination: { kind: 'project_file', prefix: 'exports/tables' },
    ...(groupBy ? { group_by: groupBy } : {}),
    ...(excludeColumns.length ? { exclude_columns: excludeColumns } : {}),
  } satisfies ExportColumnTablesParams;
  return {
    action_id: 'export.column_tables',
    scope: { kind: 'project' },
    params,
    output_names: {},
    idempotency_key: freshActionRequestKey('export.column_tables'),
  };
}
