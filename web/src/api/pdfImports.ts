import type { RunEstimate } from './open';

export type PdfImportStatus =
  | 'open'
  | 'committing'
  | 'committed'
  | 'cancelling'
  | 'cancelled'
  | 'error';

export type PdfImportCategory =
  | 'text'
  | 'suspicious'
  | 'missing'
  | 'image'
  | 'other'
  | 'unreadable'
  | 'unchecked';

export type PdfImportOcrPolicy = 'missing_or_suspicious' | 'all' | 'none';

export type PdfImportOcrSelection =
  | { kind: 'engine'; engine: string }
  | { kind: 'engine_model'; engine: string; model: string | null };

export type PdfImportDestination =
  | { kind: 'new'; sheet_name: string }
  | { kind: 'existing'; sheet_id: number };

export interface PdfImportProgress {
  files_done: number;
  files_total: number;
  bytes_done?: number;
  bytes_total?: number | null;
  sealed?: boolean;
}

export interface PdfImportCleanupProgress {
  bytes_done: number;
  bytes_total: number | null;
}

export interface PdfImportFileSummary {
  occurrence_id: string;
  logical_path: string;
  filename: string;
  size: number;
  page_count: number | null;
  category: PdfImportCategory;
  analysis_state: 'pending' | 'done' | 'error';
  upload_state?: 'pending' | 'staged';
  sample_pages: number[];
  thumbnail_url: string | null;
  error?: string | null;
}

export interface PdfImportChoices {
  ocr_policy: PdfImportOcrPolicy;
  ocr_selection: PdfImportOcrSelection;
  semantic_search: boolean;
  semantic_setup_confirmation: string | null;
  destination: PdfImportDestination;
}

export interface PdfImportBatchOcr {
  state: 'idle' | 'ready' | 'error';
  advisory?: boolean;
  purpose?: 'batch_pdf_ocr' | null;
  ocr_action_params?: Record<string, unknown> | null;
  options?: Record<string, unknown> | null;
  ocr_policy: PdfImportOcrPolicy | null;
  manifest_revision: number | null;
  manifest_hash: string | null;
  selected_through: number;
  selected_count: number;
  known_pages?: number;
  work_scope_hash?: string | null;
  estimate: RunEstimate | null;
  needs_confirmation: boolean;
  error?: string | null;
}

export interface PdfImportMatch {
  state: 'idle' | 'running' | 'ready' | 'error';
  checked: number;
  total: number;
  matched_count: number;
  new_count: number;
  new_category_counts?: Partial<Record<PdfImportCategory, number>>;
  error?: string | null;
}

export interface PdfImportSemanticSetup {
  setup_ref: string | null;
  display_name: string;
  ready: boolean;
  runtime_ready: boolean;
  download_bytes: number | null;
  additional_runtime_download: boolean;
  needs_confirmation: boolean;
  confirmation_hash: string | null;
  license: string | null;
  source_url: string | null;
  available: boolean;
}

export interface PdfImportCommitResult {
  sheet_id: number;
  job_id: string;
}

export interface PdfImportSnapshot {
  schema_version: string;
  id: string;
  draft_id: string;
  revision: number;
  updated_at: string;
  state: PdfImportStatus;
  paused: boolean;
  created_at: string;
  source_label: string | null;
  preflight: { state: 'waiting' | 'ready' | 'insufficient'; free_bytes: number | null };
  manifest: PdfImportProgress;
  upload: PdfImportProgress;
  analysis: PdfImportProgress;
  cleanup: PdfImportCleanupProgress;
  category_counts: Partial<Record<PdfImportCategory, number>>;
  sample_ready_counts: Partial<Record<PdfImportCategory, number>>;
  can_review_text: boolean;
  choices: PdfImportChoices;
  match: PdfImportMatch;
  semantic_setup: PdfImportSemanticSetup;
  batch_ocr?: PdfImportBatchOcr | null;
  preview_jobs?: PdfImportPreviewJob[];
  commit?: {
    commit_id: string;
    idempotency_key: string;
    result?: { sheet_id?: number } | null;
    [key: string]: unknown;
  } | null;
  error?: string | null;
  expires_at?: string;
}

export interface PdfImportPage {
  schema_version: string;
  draft_id: string;
  import_id?: string;
  occurrence_id: string;
  page: number;
  native_text: string;
  ocr_text: string | null;
  ocr_blocks?: Array<Record<string, unknown>>;
  ocr_action_params: Record<string, unknown> | null;
  thumbnail_url: string;
}

export interface PdfImportPreviewEstimate {
  schema_version?: string;
  draft_id?: string;
  occurrence_id?: string;
  page?: number;
  ocr_action_params: Record<string, unknown>;
  cached: boolean;
  estimate: RunEstimate | null;
}

export interface PdfImportJobRef {
  schema_version: string;
  import_id: string;
  job_id: string;
  kind: 'ocr_preview' | 'commit' | 'discard';
  sheet_id?: number | null;
}

export interface PdfImportPreviewJob {
  occurrence_id: string;
  page: number;
  ocr_action_params: Record<string, unknown>;
  job_id: string;
  status: string;
  receipt_id: string | null;
  error?: string | null;
}

export interface PdfImportPreviewStart {
  schema_version: string;
  draft_id: string;
  occurrence_id: string;
  page: number;
  ocr_action_params: Record<string, unknown>;
  job_id: string | null;
  status: 'running' | 'done';
  receipt_id: string | null;
  estimate: RunEstimate | null;
  needs_confirmation: boolean;
}

export interface PdfImportRequestOptions {
  signal?: AbortSignal;
  fetch?: typeof globalThis.fetch;
}

export interface PdfImportUpload {
  fileId: string;
  file: File;
  logicalPath?: string;
}

export interface PdfImportCreateFile {
  client_file_id: string;
  logical_path: string;
  filename: string;
  size: number;
}

export interface PdfImportSamples {
  items: PdfImportFileSummary[];
  next_cursor: string | null;
}

export interface PdfImportReselectFile {
  logical_path: string;
  filename: string;
  size: number;
}

export interface PdfImportReselectResult extends PdfImportReselectFile {
  matched: boolean;
  occurrence_id: string | null;
  upload_needed: boolean;
}

export interface PdfImportReselectResponse {
  schema_version: string;
  draft_id: string;
  items: PdfImportReselectResult[];
}

export interface PdfImportBatchOcrEstimate {
  state: 'idle' | 'ready' | 'error';
  advisory?: boolean;
  purpose?: 'batch_pdf_ocr' | null;
  ocr_action_params?: Record<string, unknown> | null;
  options?: Record<string, unknown> | null;
  ocr_policy: PdfImportOcrPolicy | null;
  manifest_revision: number | null;
  manifest_hash: string | null;
  selected_through: number;
  selected_count: number;
  known_pages?: number;
  work_scope_hash?: string | null;
  estimate: RunEstimate | null;
  needs_confirmation: boolean;
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

export interface PdfImportCommitInput {
  idempotency_key: string;
  expected_revision: number;
}

export interface PdfImportCommitResponse {
  schema_version: string;
  draft_id: string;
  commit_id: string;
  status: 'accepted';
  manifest_hash: string;
  selected_total: number;
  available_through: number;
  uploads_complete: boolean;
  job: { sheet_id?: number; import_ref?: string; [key: string]: unknown } | null;
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

async function request<Response>(
  path: string,
  init: RequestInit,
  implementation: typeof globalThis.fetch = globalThis.fetch,
): Promise<Response> {
  const response = await implementation(path, init);
  let payload: unknown;
  try {
    payload = await response.json();
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    payload = null;
  }
  if (!response.ok) throw new PdfImportResponseError(response.status, payload);
  return payload as Response;
}

function jsonRequest<Response>(
  path: string,
  method: string,
  body: unknown,
  options: PdfImportRequestOptions,
): Promise<Response> {
  return request<Response>(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: options.signal,
  }, options.fetch);
}

export function createPdfImportsApi(): PdfImportsApi {
  return {
    create(projectId, expectedFiles, expectedBytes, sourceLabel, options = {}) {
      return jsonRequest(root(projectId), 'POST', {
        expected_files: expectedFiles,
        expected_bytes: expectedBytes,
        source_label: sourceLabel,
      }, options);
    },
    addManifest(projectId, importId, files, batchId, options = {}) {
      return jsonRequest(`${root(projectId)}/${encodeURIComponent(importId)}/manifest`, 'POST', {
        batch_id: batchId, files,
      }, options);
    },
    sealManifest(projectId, importId, options = {}) {
      return request(`${root(projectId)}/${encodeURIComponent(importId)}/manifest/seal`, {
        method: 'POST', signal: options.signal,
      }, options.fetch);
    },
    async list(projectId, options = {}) {
      const response = await request<PdfImportSnapshot[] | { items: PdfImportSnapshot[] }>(
        root(projectId), { method: 'GET', signal: options.signal }, options.fetch,
      );
      return Array.isArray(response) ? response : response.items;
    },
    get(projectId, importId, options = {}) {
      return request(`${root(projectId)}/${encodeURIComponent(importId)}`, {
        method: 'GET', signal: options.signal,
      }, options.fetch);
    },
    samples(projectId, importId, category, cursor, options = {}) {
      const query = new URLSearchParams({ category, limit: '6' });
      if (cursor) query.set('cursor', cursor);
      return request(`${root(projectId)}/${encodeURIComponent(importId)}/samples?${query.toString()}`, {
        method: 'GET', signal: options.signal,
      }, options.fetch);
    },
    reconcileReselection(projectId, importId, files, options = {}) {
      return jsonRequest(`${root(projectId)}/${encodeURIComponent(importId)}/reselect`, 'POST', { files }, options);
    },
    upload(projectId, importId, files, batchId, options = {}) {
      const body = new FormData();
      for (const item of files) body.append('files', item.file);
      for (const item of files) body.append('file_ids', item.fileId);
      for (const item of files) body.append('logical_paths', item.logicalPath || item.file.webkitRelativePath || item.file.name);
      body.append('batch_id', batchId);
      return request(`${root(projectId)}/${encodeURIComponent(importId)}/uploads`, {
        method: 'POST', body, signal: options.signal,
      }, options.fetch);
    },
    pause(projectId, importId, options = {}) {
      return request(`${root(projectId)}/${encodeURIComponent(importId)}/pause`, {
        method: 'POST', signal: options.signal,
      }, options.fetch);
    },
    resume(projectId, importId, options = {}) {
      return request(`${root(projectId)}/${encodeURIComponent(importId)}/resume`, {
        method: 'POST', signal: options.signal,
      }, options.fetch);
    },
    updateChoices(projectId, importId, choices, options = {}) {
      return jsonRequest(`${root(projectId)}/${encodeURIComponent(importId)}/choices`, 'PATCH', choices, options);
    },
    refreshSemanticSetup(projectId, importId, options = {}) {
      return request(`${root(projectId)}/${encodeURIComponent(importId)}/semantic-setup/refresh`, {
        method: 'POST', signal: options.signal,
      }, options.fetch);
    },
    estimateBatchOcr(projectId, importId, selection, ocrPolicy, expectedRevision, options = {}) {
      return jsonRequest(`${root(projectId)}/${encodeURIComponent(importId)}/ocr-estimate`, 'POST', {
        ocr_selection: selection, ocr_policy: ocrPolicy, expected_revision: expectedRevision,
      }, options);
    },
    match(projectId, importId, options = {}) {
      return request(`${root(projectId)}/${encodeURIComponent(importId)}/match`, {
        method: 'POST', signal: options.signal,
      }, options.fetch);
    },
    getPage(projectId, importId, fileId, page, options = {}) {
      return request(`${root(projectId)}/${encodeURIComponent(importId)}/files/${encodeURIComponent(fileId)}/pages/${page}`, {
        method: 'GET', signal: options.signal,
      }, options.fetch);
    },
    thumbnailUrl(projectId, importId, fileId, page) {
      return `${root(projectId)}/${encodeURIComponent(importId)}/files/${encodeURIComponent(fileId)}/pages/${page}/thumbnail`;
    },
    estimateOcrPreview(projectId, importId, fileId, page, selection, options = {}) {
      return jsonRequest(`${root(projectId)}/${encodeURIComponent(importId)}/files/${encodeURIComponent(fileId)}/pages/${page}/ocr-preview/estimate`, 'POST', { ocr_selection: selection }, options);
    },
    startOcrPreview(projectId, importId, fileId, page, selection, confirmation, options = {}) {
      return jsonRequest(`${root(projectId)}/${encodeURIComponent(importId)}/files/${encodeURIComponent(fileId)}/pages/${page}/ocr-preview`, 'POST', { ocr_selection: selection, confirmation }, options);
    },
    discard(projectId, importId, options = {}) {
      return request(`${root(projectId)}/${encodeURIComponent(importId)}`, {
        method: 'DELETE', signal: options.signal,
      }, options.fetch);
    },
    commit(projectId, importId, input, options = {}) {
      return jsonRequest(`${root(projectId)}/${encodeURIComponent(importId)}/commit`, 'POST', input, options);
    },
  };
}

export const pdfImportsApi = createPdfImportsApi();
