import { httpContract } from './httpContract';
import type { HttpExtractionDocumentResponse, HttpExtractionPreviewRequest, HttpExtractionPreviewResponse, HttpExtractionSavedLayout, HttpExtractionTemplateSave, HttpExtractionLayoutSelection, HttpExtractionScopeCountsRequest } from '../generated/openHttpContracts';

export type ExtractionDocument = HttpExtractionDocumentResponse;
type PreviewDocumentWire = HttpExtractionPreviewResponse['documents'][number];
type CellWire = PreviewDocumentWire['result']['records'][number]['cells'][string];
export type ExtractedCell = Required<CellWire>;
export type ExtractionPreviewDocument = Omit<PreviewDocumentWire, 'result'> & { result: Omit<PreviewDocumentWire['result'], 'records' | 'diagnostics'> & {
  diagnostics: NonNullable<PreviewDocumentWire['result']['diagnostics']>;
  records: Array<{ cells: Record<string, ExtractedCell> }>;
} };
export type ExtractionPreview = Omit<HttpExtractionPreviewResponse, 'documents'> & { documents: ExtractionPreviewDocument[] };
export type ExtractionRequest = HttpExtractionPreviewRequest;
export type SavedExtractionTemplate = HttpExtractionSavedLayout;
export type ExtractionScope = ExtractionRequest['scope'];
export type ExtractionScopeFilter = Pick<ExtractionScope, 'filter' | 'parent_row_id' | 'scope_row_ids'>;

export const documentExtractionApi = {
  document: (projectId: string, sheetId: string, columnId: string, rowId: string, signal?: AbortSignal) =>
    httpContract('tenant.extraction_document_get.get', { pathParams: { pid: projectId, row_id: Number(rowId) }, query: { sheet_id: Number(sheetId), column_id: Number(columnId) }, signal }),
  templates: (projectId: string, sheetId: string, source: string, signal?: AbortSignal) =>
    httpContract('tenant.extraction_templates_list.get', { pathParams: { pid: projectId }, query: { sheet_id: Number(sheetId), source }, signal }),
  save: (projectId: string, value: HttpExtractionTemplateSave) =>
    httpContract('tenant.extraction_template_save.post', { pathParams: { pid: projectId }, query: {}, body: value }),
  select: (projectId: string, value: HttpExtractionLayoutSelection) =>
    httpContract('tenant.extraction_layout_select.post', { pathParams: { pid: projectId }, query: {}, body: value }),
  counts: (projectId: string, value: HttpExtractionScopeCountsRequest, signal?: AbortSignal) =>
    httpContract('tenant.extraction_scope_counts.post', { pathParams: { pid: projectId }, query: {}, body: value, signal }),
  preview: async (projectId: string, value: ExtractionRequest, signal?: AbortSignal): Promise<ExtractionPreview> => {
    const result = await httpContract('tenant.extraction_preview.post', { pathParams: { pid: projectId }, query: {}, body: value, signal });
    return { ...result, documents: result.documents.map((document) => ({ ...document, result: {
      ...document.result, diagnostics: document.result.diagnostics ?? [], records: document.result.records.map((record) => ({ cells:
        Object.fromEntries(Object.entries(record.cells).map(([id, cell]) => [id, { ...cell, regions: cell.regions ?? [], diagnostic: cell.diagnostic ?? null }])) })),
    } })) };
  },
};
