import { freshActionRequestKey, type RegisteredActionRequest } from '../api/types';
import type { GeneratedActionParams } from '../generated/actionTypes';

export function pdfTablesMaterializeRequest(value: {
  sheetId: string;
  columnId: string;
  includeColumns: readonly string[];
  targetName: string;
}): RegisteredActionRequest {
  const sheetId = Number(value.sheetId);
  const columnId = Number(value.columnId);
  if (![sheetId, columnId].every((id) => Number.isSafeInteger(id) && id > 0)) {
    throw new Error('Choose an existing extracted-table column.');
  }
  const params = {
    source: { kind: 'column', sheet_id: sheetId, column_id: columnId,
      include_columns: [...value.includeColumns] },
  } satisfies GeneratedActionParams['derive.table_from_list'];
  return {
    action_id: 'derive.table_from_list',
    scope: { kind: 'project' },
    sheet_name: value.targetName,
    params,
    output_names: {},
    idempotency_key: freshActionRequestKey('derive.table_from_list'),
  };
}
