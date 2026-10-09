import type { RunEstimate } from './open';
import { requestPdfPacketSplit } from './raw/pdfPacketSplitsTransport';

export type PdfPacketSplitStatus =
  | 'preparing'
  | 'ready'
  | 'committing'
  | 'completed'
  | 'error'
  | 'cancelled';

export interface PdfPacketInfo {
  filename: string;
  blob_hash: string;
  mime: 'application/pdf';
  page_count: number | null;
  size: number;
}

export interface PdfPacketProgress {
  status: 'queued' | 'running' | 'done' | 'error' | 'cancelled';
  done: number;
  total: number | null;
  error: string | null;
}

export interface PdfPacketJobState {
  job_id: string;
  kind: 'prepare' | 'ocr_sample' | 'ocr_full' | 'commit';
  progress: PdfPacketProgress;
  engine: string | null;
  pages: number[];
  receipt_id: string | null;
  accounting: Record<string, unknown> | null;
}

export interface PdfPacketSplitSnapshot {
  schema_version: 'frisket.pdf_packet_split.v1';
  split_id: string;
  status: PdfPacketSplitStatus;
  packet: PdfPacketInfo;
  prepare: {
    job_id: string;
    progress: PdfPacketProgress;
    pages_ready: number[];
    native_text_pages: number[];
    visual_pages_ready: number;
  };
  text_source: 'unconfirmed' | 'native' | 'ocr';
  ocr_engine: string | null;
  jobs: PdfPacketJobState[];
  analysis_revision: number;
  expires_at: string;
  commit_result: { sheet_id: number; document_count: number; receipt_id: string | null } | null;
}

export interface PdfPacketPage {
  schema_version: 'frisket.pdf_packet_split.v1';
  split_id: string;
  page: number;
  thumbnail_ready: boolean;
  thumbnail_url: string;
  native_text: string | null;
  ocr_text: string | null;
  ocr_blocks: Array<Record<string, unknown>>;
  ocr_engine: string | null;
}

export interface PdfPacketPhrase {
  id: string;
  text: string;
  enabled: boolean;
  fuzzy: boolean;
}

export interface PdfPacketPageMatch {
  page: number;
  visual_score: number | null;
  closest_confirmed_page: number | null;
  kind_id: number | null;
  matched_phrase_ids: string[];
  suggested: boolean;
}

export interface PdfPacketMatchResult {
  schema_version: 'frisket.pdf_packet_split.v1';
  analysis_revision: number;
  split_id: string;
  clusters: Array<{ id: number; confirmed_pages: number[] }>;
  pages: PdfPacketPageMatch[];
  suggested_pages: number[];
  question_pages: number[];
  phrase_counts: Record<string, number>;
  accept_all_scope: 'packet';
}

export interface PdfPacketCommitResult {
  schema_version: 'frisket.pdf_packet_split.v1';
  split_id: string;
  job_id: string;
  kind: 'ocr_sample' | 'ocr_full' | 'commit';
  total: number;
  receipt_id: string | null;
}

export interface PdfPacketRequestOptions {
  signal?: AbortSignal;
}

const prefix = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/import/pdf-packet-splits`;

async function responseJson<T>(response: Response): Promise<T> {
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body && typeof body === 'object' && 'detail' in body
      ? String((body as { detail: unknown }).detail)
      : `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return body as T;
}

async function jsonRequest<T>(
  url: string,
  method: 'POST' | 'PUT' | 'DELETE',
  body: unknown,
  options: PdfPacketRequestOptions = {},
): Promise<T> {
  return responseJson<T>(await requestPdfPacketSplit(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: options.signal,
  }));
}

export async function createPdfPacketSplit(
  projectId: string,
  file: File,
  requestId: string = crypto.randomUUID(),
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketSplitSnapshot> {
  const form = new FormData();
  form.append('file', file);
  form.append('request_id', requestId);
  return responseJson(await requestPdfPacketSplit(prefix(projectId), {
    method: 'POST',
    body: form,
    signal: options.signal,
  }));
}

export function getPdfPacketSplit(
  projectId: string,
  splitId: string,
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketSplitSnapshot> {
  return requestPdfPacketSplit(`${prefix(projectId)}/${encodeURIComponent(splitId)}`, {
    signal: options.signal,
  }).then(responseJson<PdfPacketSplitSnapshot>);
}

export function getPdfPacketPage(
  projectId: string,
  splitId: string,
  page: number,
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketPage> {
  return requestPdfPacketSplit(`${prefix(projectId)}/${encodeURIComponent(splitId)}/pages/${page}`, {
    signal: options.signal,
  }).then(responseJson<PdfPacketPage>);
}

export function pdfPacketThumbnailUrl(projectId: string, splitId: string, page: number): string {
  return `${prefix(projectId)}/${encodeURIComponent(splitId)}/pages/${page}/thumbnail`;
}

export function estimatePdfPacketOcr(
  projectId: string,
  splitId: string,
  input: { engine: string; scope: 'sample' | 'all'; pages?: number[] },
  options?: PdfPacketRequestOptions,
): Promise<{ schema_version: string; engine: string; scope: 'sample' | 'all'; pages: number[]; cached_pages: number[]; estimate: RunEstimate }> {
  return jsonRequest(`${prefix(projectId)}/${encodeURIComponent(splitId)}/ocr/estimate`, 'POST', input, options);
}

export function startPdfPacketOcr(
  projectId: string,
  splitId: string,
  input: { engine: string; scope: 'sample' | 'all'; pages?: number[]; confirmation?: string },
  options?: PdfPacketRequestOptions,
): Promise<PdfPacketCommitResult> {
  return jsonRequest(`${prefix(projectId)}/${encodeURIComponent(splitId)}/ocr/jobs`, 'POST', input, options);
}

export function cancelPdfPacketOcr(
  projectId: string,
  splitId: string,
  jobId: string,
  options?: PdfPacketRequestOptions,
): Promise<PdfPacketSplitSnapshot> {
  return jsonRequest(`${prefix(projectId)}/${encodeURIComponent(splitId)}/jobs/${encodeURIComponent(jobId)}`, 'DELETE', undefined, options);
}

export function setPdfPacketTextSource(
  projectId: string,
  splitId: string,
  source: { kind: 'native'; engine?: never } | { kind: 'ocr'; engine: string },
  options?: PdfPacketRequestOptions,
): Promise<PdfPacketSplitSnapshot> {
  return jsonRequest(`${prefix(projectId)}/${encodeURIComponent(splitId)}/text-source`, 'PUT', source, options);
}

export function matchPdfPacketCandidates(
  projectId: string,
  splitId: string,
  input: {
    confirmed_starts: number[];
    rejected: number[];
    threshold_pct: number;
    phrases: PdfPacketPhrase[];
  },
  options?: PdfPacketRequestOptions,
): Promise<PdfPacketMatchResult> {
  return jsonRequest(`${prefix(projectId)}/${encodeURIComponent(splitId)}/candidates`, 'POST', input, options);
}

export function commitPdfPacketSplit(
  projectId: string,
  splitId: string,
  input: {
    confirmed_starts: number[];
    destination: { kind: 'new_sheet'; name: string };
    name_pattern: string;
    keep_ocr_text: boolean;
    idempotency_key: string;
  },
  options?: PdfPacketRequestOptions,
): Promise<PdfPacketCommitResult> {
  return jsonRequest(`${prefix(projectId)}/${encodeURIComponent(splitId)}/commit`, 'POST', input, options);
}

export function deletePdfPacketSplit(
  projectId: string,
  splitId: string,
  options?: PdfPacketRequestOptions,
): Promise<void> {
  return requestPdfPacketSplit(`${prefix(projectId)}/${encodeURIComponent(splitId)}`, {
    method: 'DELETE',
    signal: options?.signal,
  }).then(async (response) => {
    if (!response.ok) await responseJson(response);
  });
}

