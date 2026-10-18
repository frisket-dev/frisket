import { httpContract } from './httpContract';

export interface DocumentPriorityTarget {
  sheetId: number;
  rowId: number;
  columnId: number;
}

export interface DocumentPriorityResult {
  promoted: boolean;
  runId: string | null;
}

/** Best-effort hint emitted only by the explicit document-open command. */
export async function promoteDocumentPriority(
  projectId: string,
  target: DocumentPriorityTarget,
): Promise<DocumentPriorityResult> {
  const response = await httpContract('tenant.promote_document_priority.post', {
    pathParams: { pid: projectId },
    query: {},
    body: {
      sheet_id: target.sheetId,
      row_id: target.rowId,
      column_id: target.columnId,
    },
  });
  return {
    promoted: response.promoted,
    runId: response.run_id == null ? null : String(response.run_id),
  };
}
