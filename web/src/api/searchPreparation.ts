export type SearchPreparationStatus =
  | 'planning'
  | 'needs_consent'
  | 'preparing'
  | 'complete'
  | 'stopped'
  | 'failed';

export type SearchPreparationGroupKind = 'native_pdf' | 'ocr' | 'transcribe';

export interface SearchPreparationGroup {
  kind: SearchPreparationGroupKind;
  count: number;
  status:
    | 'planning'
    | 'needs_consent'
    | 'waiting_for_download'
    | 'queued'
    | 'running'
    | 'complete'
    | 'not_prepared'
    | 'stopped'
    | 'failed';
}

export interface SearchPreparationModel {
  setup_ref: string;
  display_name: string;
  purpose: string;
  required: boolean;
  ready: boolean;
  runtime_ready: boolean;
  download_bytes: number | null;
  additional_runtime_download: boolean;
  remediation: string | null;
  pull_id: number | null;
  license?: string | null;
  source_url?: string | null;
}

export interface SearchPreparationJob {
  job_id: number;
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled';
  action_kind: string | null;
}

export interface SearchPreparationBatch {
  schemaVersion: 'frisket.search_preparation.v1';
  id: string;
  sheet_id: number;
  plan_hash: string;
  status: SearchPreparationStatus;
  groups: SearchPreparationGroup[];
  models: SearchPreparationModel[];
  jobs: SearchPreparationJob[];
  can_stop: boolean;
  can_setup: boolean;
  errors: string[];
}

export interface SearchPreparationTextSample {
  row_id: number;
  column_id: number;
  filename: string | null;
  page_number: number;
  page_count?: number;
  preview_url?: string | null;
  text: string;
  source: 'native' | 'prepared';
  truncated: boolean;
}

export interface SearchPreparationRequestOptions {
  signal?: AbortSignal;
}

export interface CreateSearchPreparationInput {
  sheet_id: number;
  row_ids?: number[];
  force_ocr?: boolean;
}

export interface DecideSearchPreparationInput {
  plan_hash: string;
  decision: 'prepare' | 'not_now';
  selected_setup_refs: string[];
}

function prefix(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/search-preparation`;
}

function errorMessage(status: number, payload: unknown): string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = payload.detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string') {
      return detail.message;
    }
  }
  return `Search preparation request failed (HTTP ${status})`;
}

async function requestJson<Response>(
  url: string,
  init: RequestInit,
): Promise<Response> {
  // New operation IDs land when the integration owner regenerates the HTTP
  // artifact from the backend routes. Keep this one adapter isolated until
  // then; components never call fetch directly.
  // eslint-disable-next-line no-restricted-syntax
  const response = await fetch(url, init);
  const payload: unknown = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorMessage(response.status, payload));
  return payload as Response;
}

function jsonRequest(method: 'POST', body?: unknown, signal?: AbortSignal): RequestInit {
  return {
    method,
    signal,
    headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  };
}

export function createSearchPreparation(
  projectId: string,
  input: CreateSearchPreparationInput,
  options: SearchPreparationRequestOptions = {},
): Promise<SearchPreparationBatch> {
  return requestJson(prefix(projectId), jsonRequest('POST', input, options.signal));
}

export function listSearchPreparations(
  projectId: string,
  options: SearchPreparationRequestOptions = {},
): Promise<{ batches: SearchPreparationBatch[] }> {
  return requestJson(prefix(projectId), { signal: options.signal });
}

export function getSearchPreparation(
  projectId: string,
  batchId: string,
  options: SearchPreparationRequestOptions = {},
): Promise<SearchPreparationBatch> {
  return requestJson(
    `${prefix(projectId)}/${encodeURIComponent(batchId)}`,
    { signal: options.signal },
  );
}

export function decideSearchPreparation(
  projectId: string,
  batchId: string,
  input: DecideSearchPreparationInput,
  options: SearchPreparationRequestOptions = {},
): Promise<SearchPreparationBatch> {
  return requestJson(
    `${prefix(projectId)}/${encodeURIComponent(batchId)}/decision`,
    jsonRequest('POST', input, options.signal),
  );
}

export function stopSearchPreparation(
  projectId: string,
  batchId: string,
  options: SearchPreparationRequestOptions = {},
): Promise<SearchPreparationBatch> {
  return requestJson(
    `${prefix(projectId)}/${encodeURIComponent(batchId)}/stop`,
    jsonRequest('POST', undefined, options.signal),
  );
}

export function getSearchPreparationTextSamples(
  projectId: string,
  batchId: string,
  selection: { rowId?: number; pageNumber?: number } = {},
  options: SearchPreparationRequestOptions = {},
): Promise<{
  schemaVersion: 'frisket.search_preparation_text_samples.v1';
  batch_id: string;
  samples: SearchPreparationTextSample[];
}> {
  const query = new URLSearchParams();
  if (selection.rowId != null) query.set('row_id', String(selection.rowId));
  if (selection.pageNumber != null) query.set('page_number', String(selection.pageNumber));
  const suffix = query.size ? `?${query.toString()}` : '';
  return requestJson(
    `${prefix(projectId)}/${encodeURIComponent(batchId)}/text-samples${suffix}`,
    { signal: options.signal },
  );
}
