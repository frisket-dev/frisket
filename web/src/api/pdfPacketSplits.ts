import type { RunEstimate } from './open';
import { runEstimateFromV1Wire } from './actionEstimateValidation';
import { httpContract } from './httpContract';
import type {
  HttpPdfPacketBatchOcrEstimateRequest,
  HttpPdfPacketDraftCreateRequest,
  HttpPdfPacketSheetCreateRequest,
  HttpPdfPacketSheetScopeRequest,
  HttpPdfPacketCandidatesRequest,
  HttpPdfPacketCandidatesResponse,
  HttpPdfPacketCommitRequest,
  HttpPdfPacketJobStartResponse,
  HttpPdfPacketOcrEstimateRequest,
  HttpPdfPacketOcrEstimateResponse,
  HttpPdfPacketOcrJobRequest,
  HttpPdfPacketPageResponse,
  HttpPdfPacketSplitStatus,
  HttpPdfPacketTextSourceRequest,
} from '../generated/openHttpContracts';

export type PdfPacketSplitSnapshot = HttpPdfPacketSplitStatus;
export type PdfPacketSplitStatus = PdfPacketSplitSnapshot['status'];
export type PdfPacketInfo = PdfPacketSplitSnapshot['packet'];
export type PdfPacketProgress = PdfPacketSplitSnapshot['prepare']['progress'];
export type PdfPacketJobState = PdfPacketSplitSnapshot['jobs'][number];
export type PdfPacketPage = HttpPdfPacketPageResponse;
export type PdfPacketPhrase = Required<NonNullable<HttpPdfPacketCandidatesRequest['phrases']>[number]>;
export type PdfPacketPageMatch = HttpPdfPacketCandidatesResponse['pages'][number];
export type PdfPacketMatchResult = HttpPdfPacketCandidatesResponse;
export type PdfPacketCommitResult = HttpPdfPacketJobStartResponse;

export type PdfPacketOcrEstimate = Omit<HttpPdfPacketOcrEstimateResponse, 'estimate'> & {
  estimate: RunEstimate | null;
};

export interface PdfPacketRequestOptions {
  signal?: AbortSignal;
}

const prefix = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/import/pdf-packet-splits`;

export function createPdfPacketSplit(
  projectId: string,
  file: File,
  requestId: string = crypto.randomUUID(),
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketSplitSnapshot> {
  const body = new FormData();
  body.append('file', file);
  body.append('request_id', requestId);
  return httpContract('tenant.create_pdf_packet_split.post', {
    pathParams: { pid: projectId },
    query: {},
    body,
    signal: options.signal,
  });
}

export function getPdfPacketSplit(
  projectId: string,
  splitId: string,
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketSplitSnapshot> {
  return httpContract('tenant.get_pdf_packet_split.get', {
    pathParams: { pid: projectId, split_id: splitId },
    query: {},
    signal: options.signal,
  });
}

export function getPdfPacketPage(
  projectId: string,
  splitId: string,
  page: number,
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketPage> {
  return httpContract('tenant.get_pdf_packet_split_page.get', {
    pathParams: { pid: projectId, split_id: splitId, page },
    query: {},
    signal: options.signal,
  });
}

export function pdfPacketSourceUrl(projectId: string, splitId: string, sourceKey: string): string {
  const query = new URLSearchParams({ source_key: sourceKey });
  return `${prefix(projectId)}/${encodeURIComponent(splitId)}/source-file?${query.toString()}`;
}

export function pdfPacketThumbnailUrl(projectId: string, splitId: string, page: number): string {
  return `${prefix(projectId)}/${encodeURIComponent(splitId)}/pages/${page}/thumbnail`;
}

export function estimatePdfPacketOcr(
  projectId: string,
  splitId: string,
  input: HttpPdfPacketOcrEstimateRequest,
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketOcrEstimate> {
  return httpContract(
    'tenant.estimate_pdf_packet_split_ocr.post',
    {
      pathParams: { pid: projectId, split_id: splitId },
      query: {},
      body: input,
      signal: options.signal,
    },
    (wire) => ({
      ...wire,
      estimate: wire.estimate === null ? null : runEstimateFromV1Wire(wire.estimate),
    }),
  );
}

export function startPdfPacketOcr(
  projectId: string,
  splitId: string,
  input: HttpPdfPacketOcrJobRequest,
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketCommitResult> {
  return httpContract('tenant.start_pdf_packet_split_ocr.post', {
    pathParams: { pid: projectId, split_id: splitId },
    query: {},
    body: input,
    signal: options.signal,
  });
}

export function cancelPdfPacketOcr(
  projectId: string,
  splitId: string,
  jobId: string,
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketSplitSnapshot> {
  return httpContract('tenant.cancel_pdf_packet_split_job.delete', {
    pathParams: { pid: projectId, split_id: splitId, job_id: jobId },
    query: {},
    signal: options.signal,
  });
}

export function setPdfPacketTextSource(
  projectId: string,
  splitId: string,
  source: HttpPdfPacketTextSourceRequest,
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketSplitSnapshot> {
  return httpContract('tenant.select_pdf_packet_split_text_source.put', {
    pathParams: { pid: projectId, split_id: splitId },
    query: {},
    body: source,
    signal: options.signal,
  });
}

export function matchPdfPacketCandidates(
  projectId: string,
  splitId: string,
  input: HttpPdfPacketCandidatesRequest,
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketMatchResult> {
  return httpContract('tenant.match_pdf_packet_split_candidates.post', {
    pathParams: { pid: projectId, split_id: splitId },
    query: {},
    body: input,
    signal: options.signal,
  });
}

export function commitPdfPacketSplit(
  projectId: string,
  splitId: string,
  input: HttpPdfPacketCommitRequest,
  options: PdfPacketRequestOptions = {},
): Promise<PdfPacketCommitResult> {
  return httpContract('tenant.commit_pdf_packet_split.post', {
    pathParams: { pid: projectId, split_id: splitId },
    query: {},
    body: input,
    signal: options.signal,
  });
}

export function deletePdfPacketSplit(
  projectId: string,
  splitId: string,
  options: PdfPacketRequestOptions = {},
): Promise<void> {
  return httpContract('tenant.delete_pdf_packet_split.delete', {
    pathParams: { pid: projectId, split_id: splitId },
    query: {},
    signal: options.signal,
  });
}

/** Admit the already-uploaded draft; the import draft remains the publication owner. */
export function createPdfPacketSplitFromDraft(
  projectId: string, input: HttpPdfPacketDraftCreateRequest, options: PdfPacketRequestOptions = {},
): Promise<PdfPacketSplitSnapshot> {
  return httpContract('tenant.create_pdf_packet_split_from_draft.post', {
    pathParams: { pid: projectId }, query: {}, body: input, signal: options.signal,
  });
}

export type PdfPacketSheetScopeRequest = HttpPdfPacketSheetCreateRequest;

/** Admit a sheet view before reviewing it, freezing source identity and order. */
export function createPdfPacketSplitFromSheet(
  projectId: string, input: PdfPacketSheetScopeRequest, options: PdfPacketRequestOptions = {},
): Promise<PdfPacketSplitSnapshot> {
  return httpContract('tenant.create_pdf_packet_split_from_sheet.post', {
    pathParams: { pid: projectId }, query: {}, body: input, signal: options.signal,
  });
}

export async function applyPdfPacketSheetSplit(
  projectId: string, splitId: string, input: HttpPdfPacketCommitRequest, options: PdfPacketRequestOptions = {},
): Promise<{ sheet_id: number; import_ref: string; job_id: number }> {
  const result = await commitPdfPacketSplit(projectId, splitId, input, options);
  if (typeof result.sheet_id !== 'number' || typeof result.import_ref !== 'string' || typeof result.job_id !== 'number') {
    throw new Error('The split did not return a durable import job.');
  }
  return { sheet_id: result.sheet_id, import_ref: result.import_ref, job_id: result.job_id };
}

export function estimatePdfPacketBatchOcr(
  projectId: string, splitId: string, input: HttpPdfPacketBatchOcrEstimateRequest, options: PdfPacketRequestOptions = {},
): Promise<{ pages: number; estimate: RunEstimate | null }> {
  return httpContract('tenant.estimate_pdf_packet_split_batch_ocr.post', {
    pathParams: { pid: projectId, split_id: splitId }, query: {}, body: input, signal: options.signal,
  }, (wire) => ({ pages: wire.pages, estimate: wire.estimate ? runEstimateFromV1Wire(wire.estimate) : null }));
}

/** Snapshot only the ordered view membership; this does not prepare PDFs. */
export function getPdfPacketSheetScopeToken(
  projectId: string, input: HttpPdfPacketSheetScopeRequest, options: PdfPacketRequestOptions = {},
): Promise<{ order_hash: string }> {
  return httpContract('tenant.get_pdf_packet_sheet_scope_token.post', {
    pathParams: { pid: projectId }, query: {}, body: input, signal: options.signal,
  });
}
