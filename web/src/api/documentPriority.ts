import { httpContract } from './httpContract';

export interface DocumentPriorityTarget {
  sheetId: number;
  rowId: number;
  columnId: number;
}

/** Best-effort hint emitted only by the explicit document-open command. */
export async function promoteDocumentPriority(
  projectId: string,
  target: DocumentPriorityTarget,
): Promise<{ promoted: boolean }> {
  return httpContract('tenant.promote_document_priority.post', {
    pathParams: { pid: projectId },
    query: {},
    body: {
      sheet_id: target.sheetId,
      row_id: target.rowId,
      column_id: target.columnId,
    },
  });
}
