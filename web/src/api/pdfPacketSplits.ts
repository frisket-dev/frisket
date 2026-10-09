import type { RunEstimate } from './open';
import { runEstimateFromV1Wire } from './actionEstimateValidation';
import { httpContract } from './httpContract';
import type {
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
