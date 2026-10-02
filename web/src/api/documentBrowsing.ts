import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type { DocumentListOptions, DocumentListPage } from './types';

type DocumentPageWire = HttpContractSuccessResponse<'tenant.browse_sheet_documents.get'>;
type ContractErrorFactory = (status: number, payload: unknown) => Error;

function mapPage(page: DocumentPageWire): DocumentListPage {
  return {
    items: page.items.map((item) => ({
      rowId: String(item.row_id),
      ordinal: item.ordinal,
      title: item.title,
      titleTruncated: item.title_truncated,
      sourceKind: item.source_kind,
      sourcePresent: item.source_present,
      sourceLabel: item.source_label,
      sourceLabelTruncated: item.source_label_truncated,
      characterCount: item.character_count ?? null,
    })),
    nextCursor: page.next_cursor,
    previousCursor: page.previous_cursor,
  };
}

export function createDocumentBrowsingApi(errorFactory: ContractErrorFactory) {
  return {
    listDocuments(projectId: string, sheetId: string, options: DocumentListOptions): Promise<DocumentListPage> {
      return httpContract('tenant.browse_sheet_documents.get', {
        pathParams: { pid: projectId, sheet_id: Number(sheetId) },
        query: {
          source_column_id: Number(options.sourceColumnId),
          title_column_id: options.titleColumnId == null ? undefined : Number(options.titleColumnId),
          parent_row_id: options.parentRowId == null ? undefined : Number(options.parentRowId),
          filter: options.filter && Object.keys(options.filter).length ? JSON.stringify(options.filter) : undefined,
          sort: options.sort?.length ? JSON.stringify(options.sort) : undefined,
          scope_row_ids: options.scopeRowIds == null ? undefined : options.scopeRowIds.join(','),
          q: options.query || undefined,
          cursor: options.cursor ?? undefined,
          anchor_row_id: options.anchorRowId == null ? undefined : Number(options.anchorRowId),
          limit: options.limit ?? 100,
        },
        signal: options.signal,
        errorFactory,
      }).then(mapPage);
    },
  };
}
