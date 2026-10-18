import type {
  HttpPdfImportBatchOcrState,
  HttpPdfImportChoicesRequest,
  HttpPdfImportCommitRequest,
  HttpPdfImportCommitResponse,
  HttpPdfImportCreateRequest,
  HttpPdfImportListResponse,
  HttpPdfImportManifestBatchRequest,
  HttpPdfImportOcrPreviewEstimate,
  HttpPdfImportOcrPreviewStart,
  HttpPdfImportPageResponse,
  HttpPdfImportReselectRequest,
  HttpPdfImportReselectResponse,
  HttpPdfImportSamplesResponse,
  HttpPdfImportStatus,
} from '../generated/openHttpContracts';
import { httpContract } from './httpContract';
import type { RunEstimate } from './open';

export type PdfImportCategory = HttpPdfImportSamplesResponse['category'];
export type PdfImportOcrPolicy = HttpPdfImportChoicesRequest['ocr_policy'];
export type PdfImportOcrSelection = HttpPdfImportChoicesRequest['ocr_selection'];
export type PdfImportDestination = HttpPdfImportChoicesRequest['destination'];
export type PdfImportChoices = Omit<HttpPdfImportChoicesRequest, 'expected_revision'>;
export type PdfImportBatchOcr = Omit<HttpPdfImportBatchOcrState, 'estimate'> & {
  estimate: RunEstimate | null;
  error?: string | null;
};
export type PdfImportFileSummary = HttpPdfImportSamplesResponse['items'][number];
export type PdfImportPage = HttpPdfImportPageResponse & { native_text_truncated?: boolean };
export type PdfImportPreviewEstimate = Omit<HttpPdfImportOcrPreviewEstimate, 'estimate'> & {
  estimate: RunEstimate | null;
};
export type PdfImportPreviewStart = HttpPdfImportOcrPreviewStart;
export type PdfImportCreateFile = HttpPdfImportManifestBatchRequest['files'][number];
export type PdfImportSamples = Pick<
  HttpPdfImportSamplesResponse,
  'items' | 'next_cursor' | 'extension_counts' | 'unlisted_extension_count'
>;
export type PdfImportReselectFile = HttpPdfImportReselectRequest['files'][number];
export type PdfImportReselectResult = HttpPdfImportReselectResponse['items'][number];
export type PdfImportReselectResponse = HttpPdfImportReselectResponse;
export type PdfImportBatchOcrEstimate = PdfImportBatchOcr;
export type PdfImportCommitInput = HttpPdfImportCommitRequest;

type PdfImportCommitRecord = NonNullable<HttpPdfImportStatus['commit']> & {
  result?: { sheet_id?: number; import_ref?: string } | null;
};

export type PdfImportSnapshot = Omit<HttpPdfImportStatus, 'batch_ocr' | 'commit'> & {
  batch_ocr: PdfImportBatchOcr;
  commit: PdfImportCommitRecord | null;
};

export type PdfImportCommitResponse = Omit<HttpPdfImportCommitResponse, 'job'> & {
  job?: ({ sheet_id?: number; import_ref?: string; [key: string]: unknown }) | null;
};

export interface PdfImportRequestOptions {
  signal?: AbortSignal;
  fetch?: typeof globalThis.fetch;
}

export interface PdfImportUpload {
  fileId: string;
  file: File;
  logicalPath?: string;
}

export interface PdfImportsApi {
  create(projectId: string, expectedFiles: number, expectedBytes: number, sourceLabel: string, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot>;
  addManifest(projectId: string, importId: string, files: PdfImportCreateFile[], batchId: string, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot>;
  sealManifest(projectId: string, importId: string, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot>;
  list(projectId: string, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot[]>;
  get(projectId: string, importId: string, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot>;
  samples(projectId: string, importId: string, category: PdfImportCategory, cursor?: string, options?: PdfImportRequestOptions): Promise<PdfImportSamples>;
  reconcileReselection(projectId: string, importId: string, files: PdfImportReselectFile[], options?: PdfImportRequestOptions): Promise<PdfImportReselectResponse>;
  upload(projectId: string, importId: string, files: PdfImportUpload[], batchId: string, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot>;
  pause(projectId: string, importId: string, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot>;
  resume(projectId: string, importId: string, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot>;
  updateChoices(projectId: string, importId: string, choices: PdfImportChoices & { expected_revision: number }, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot>;
  refreshSemanticSetup(projectId: string, importId: string, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot>;
  estimateBatchOcr(projectId: string, importId: string, selection: PdfImportOcrSelection, ocrPolicy: PdfImportOcrPolicy, expectedRevision: number, options?: PdfImportRequestOptions): Promise<PdfImportBatchOcrEstimate>;
  match(projectId: string, importId: string, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot>;
  getPage(projectId: string, importId: string, fileId: string, page: number, options?: PdfImportRequestOptions): Promise<PdfImportPage>;
  thumbnailUrl(projectId: string, importId: string, fileId: string, page: number): string;
  estimateOcrPreview(projectId: string, importId: string, fileId: string, page: number, selection: PdfImportOcrSelection, options?: PdfImportRequestOptions): Promise<PdfImportPreviewEstimate>;
  startOcrPreview(projectId: string, importId: string, fileId: string, page: number, selection: PdfImportOcrSelection, confirmation: string | undefined, options?: PdfImportRequestOptions): Promise<PdfImportPreviewStart>;
  discard(projectId: string, importId: string, options?: PdfImportRequestOptions): Promise<PdfImportSnapshot>;
  commit(projectId: string, importId: string, input: PdfImportCommitInput, options?: PdfImportRequestOptions): Promise<PdfImportCommitResponse>;
}

export class PdfImportResponseError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    const detail = typeof payload === 'object' && payload !== null && 'detail' in payload
      ? (payload as { detail?: unknown }).detail : null;
    super(typeof detail === 'string' && detail.trim() ? detail : `Request failed (HTTP ${status})`);
    this.name = 'PdfImportResponseError';
    this.status = status;
    this.payload = payload;
  }
}

const root = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/import/pdf-imports`;

function requestOptions(options: PdfImportRequestOptions) {
  return {
    signal: options.signal,
    fetch: options.fetch,
    errorFactory: (status: number, payload: unknown) => new PdfImportResponseError(status, payload),
  };
}

export function createPdfImportsApi(): PdfImportsApi {
  return {
    create(projectId, expectedFiles, expectedBytes, sourceLabel, options = {}) {
      const body: HttpPdfImportCreateRequest = {
        expected_files: expectedFiles,
        expected_bytes: expectedBytes,
        source_label: sourceLabel,
      };
      return httpContract('tenant.create_pdf_import.post', {
        pathParams: { pid: projectId }, query: {}, body, ...requestOptions(options),
      }, (wire) => wire as PdfImportSnapshot);
    },
    addManifest(projectId, importId, files, batchId, options = {}) {
      return httpContract('tenant.append_pdf_import_manifest.post', {
        pathParams: { pid: projectId, draft_id: importId }, query: {},
        body: { batch_id: batchId, files }, ...requestOptions(options),
      }, (wire) => wire as PdfImportSnapshot);
    },
    sealManifest(projectId, importId, options = {}) {
      return httpContract('tenant.seal_pdf_import_manifest.post', {
        pathParams: { pid: projectId, draft_id: importId }, query: {}, ...requestOptions(options),
      }, (wire) => wire as PdfImportSnapshot);
    },
    list(projectId, options = {}) {
      return httpContract('tenant.list_pdf_imports.get', {
        pathParams: { pid: projectId }, query: {}, ...requestOptions(options),
      }, (wire: HttpPdfImportListResponse) => wire.items as PdfImportSnapshot[]);
    },
    get(projectId, importId, options = {}) {
      return httpContract('tenant.get_pdf_import.get', {
        pathParams: { pid: projectId, draft_id: importId }, query: {}, ...requestOptions(options),
      }, (wire) => wire as PdfImportSnapshot);
    },
    samples(projectId, importId, category, cursor, options = {}) {
      return httpContract('tenant.list_pdf_import_samples.get', {
        pathParams: { pid: projectId, draft_id: importId },
        query: { category, cursor, limit: 6 }, ...requestOptions(options),
      }, (wire) => ({
        items: wire.items,
        next_cursor: wire.next_cursor,
        extension_counts: wire.extension_counts,
        unlisted_extension_count: wire.unlisted_extension_count,
      }));
    },
    reconcileReselection(projectId, importId, files, options = {}) {
      return httpContract('tenant.reconcile_pdf_import_reselection.post', {
        pathParams: { pid: projectId, draft_id: importId }, query: {}, body: { files },
        ...requestOptions(options),
      });
    },
    upload(projectId, importId, files, batchId, options = {}) {
      const body = new FormData();
      for (const item of files) body.append('files', item.file);
      for (const item of files) body.append('file_ids', item.fileId);
      for (const item of files) body.append('logical_paths', item.logicalPath || item.file.webkitRelativePath || item.file.name);
      body.append('batch_id', batchId);
      return httpContract('tenant.upload_pdf_import_files.post', {
        pathParams: { pid: projectId, draft_id: importId }, query: {}, body,
        ...requestOptions(options),
      }, (wire) => wire as PdfImportSnapshot);
    },
    pause(projectId, importId, options = {}) {
      return httpContract('tenant.pause_pdf_import.post', {
        pathParams: { pid: projectId, draft_id: importId }, query: {}, ...requestOptions(options),
      }, (wire) => wire as PdfImportSnapshot);
    },
    resume(projectId, importId, options = {}) {
      return httpContract('tenant.resume_pdf_import.post', {
        pathParams: { pid: projectId, draft_id: importId }, query: {}, ...requestOptions(options),
      }, (wire) => wire as PdfImportSnapshot);
    },
    updateChoices(projectId, importId, choices, options = {}) {
      return httpContract('tenant.update_pdf_import_choices.patch', {
        pathParams: { pid: projectId, draft_id: importId }, query: {}, body: choices,
        ...requestOptions(options),
      }, (wire) => wire as PdfImportSnapshot);
    },
    refreshSemanticSetup(projectId, importId, options = {}) {
      return httpContract('tenant.refresh_pdf_import_semantic_setup.post', {
        pathParams: { pid: projectId, draft_id: importId }, query: {}, ...requestOptions(options),
      }, (wire) => wire as PdfImportSnapshot);
    },
    estimateBatchOcr(projectId, importId, selection, ocrPolicy, expectedRevision, options = {}) {
      return httpContract('tenant.estimate_pdf_import_batch_ocr.post', {
        pathParams: { pid: projectId, draft_id: importId }, query: {},
        body: {
          ocr_selection: selection,
          ocr_policy: ocrPolicy,
          expected_revision: expectedRevision,
        },
        ...requestOptions(options),
      }, (wire) => wire as PdfImportBatchOcrEstimate);
    },
    match(projectId, importId, options = {}) {
      return httpContract('tenant.compare_pdf_import_destination.post', {
        pathParams: { pid: projectId, draft_id: importId }, query: {}, ...requestOptions(options),
      }, (wire) => wire as PdfImportSnapshot);
    },
    getPage(projectId, importId, fileId, page, options = {}) {
      return httpContract('tenant.get_pdf_import_page.get', {
        pathParams: { pid: projectId, draft_id: importId, occurrence_id: fileId, page },
        query: {}, ...requestOptions(options),
      });
    },
    thumbnailUrl(projectId, importId, fileId, page) {
      return `${root(projectId)}/${encodeURIComponent(importId)}/files/${encodeURIComponent(fileId)}/pages/${page}/thumbnail`;
    },
    estimateOcrPreview(projectId, importId, fileId, page, selection, options = {}) {
      return httpContract('tenant.estimate_pdf_import_ocr_preview.post', {
        pathParams: { pid: projectId, draft_id: importId, occurrence_id: fileId, page }, query: {},
        body: { ocr_selection: selection }, ...requestOptions(options),
      }, (wire) => wire as PdfImportPreviewEstimate);
    },
    startOcrPreview(projectId, importId, fileId, page, selection, confirmation, options = {}) {
      return httpContract('tenant.start_pdf_import_ocr_preview.post', {
        pathParams: { pid: projectId, draft_id: importId, occurrence_id: fileId, page }, query: {},
        body: { ocr_selection: selection, confirmation }, ...requestOptions(options),
      });
    },
    discard(projectId, importId, options = {}) {
      return httpContract('tenant.discard_pdf_import.delete', {
        pathParams: { pid: projectId, draft_id: importId }, query: {}, ...requestOptions(options),
      }, (wire) => wire as PdfImportSnapshot);
    },
    commit(projectId, importId, input, options = {}) {
      return httpContract('tenant.commit_pdf_import.post', {
        pathParams: { pid: projectId, draft_id: importId }, query: {}, body: input,
        ...requestOptions(options),
      });
    },
  };
}

export const pdfImportsApi = createPdfImportsApi();
