import { httpContract } from './httpContract';

interface DocumentPriorityTarget {
  sheetId: number;
  rowId: number;
  columnId: number;
}

interface DocumentPriorityResult {
  promoted: boolean;
  importRef: string | null;
}

/** Best-effort hint emitted only by the explicit document-open command. */
export async function promoteDocumentPriority(
  projectId: string,
  target: DocumentPriorityTarget,
): Promise<DocumentPriorityResult> {
  const response = await httpContract('tenant.promote_document_priority.post', {
    pathParams: { pid: projectId },
    body: {
      sheet_id: target.sheetId,
      row_id: target.rowId,
      column_id: target.columnId,
    },
  });
  return {
    promoted: response.promoted,
    importRef: response.import_ref,
  };
}
