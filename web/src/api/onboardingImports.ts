import { trackImportSubmission } from '../telemetry/importTelemetry';
import type {
  HttpContractOperationMap,
  HttpImportSessionList,
  HttpImportSessionStatus,
} from '../generated/openHttpContracts';
import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from './httpContract';
import { runEstimateFromV1Wire } from './actionEstimateValidation';
import type { RunEstimate } from './types';

export type ImportSessionStatus = HttpImportSessionStatus;
export type ImportPreparationChild = HttpImportSessionStatus['preparation_children'][number];

interface ImportPreparationRetryEstimate {
  planToken: string;
  pages: number;
  estimate: RunEstimate;
}

interface ImportSessionCreateOptions extends RequestOptions {
  sheetName?: string;
}

interface ImportSessionUploadOptions extends RequestOptions {
  logicalPaths: string[];
  batchId: string;
}

interface ImportSessionUploadSequenceOptions extends RequestOptions {
  onStatus?(status: ImportSessionStatus): void;
}

const IMPORT_SESSION_CHUNK_FILES = 256;
const IMPORT_SESSION_CHUNK_BYTES = 64 * 1024 * 1024;

interface ImportFileChunk {
  files: File[];
  logicalPaths: string[];
  start: number;
}

export function importFileLogicalPath(file: File): string {
  const relative = (file as File & { webkitRelativePath?: unknown }).webkitRelativePath;
  return typeof relative === 'string' && relative ? relative : file.name;
}

/** Browser-owned FileList/DataTransfer lists may be cleared after their event.
 * Retain one ordered array of File references; this does not copy file bytes. */
export function snapshotImportFiles(files: FileList | null | undefined): File[] {
  return files ? Array.from(files) : [];
}

/** Bounds request bodies without rejecting a single input larger than the
 * target byte budget. The server remains authoritative for deployment caps. */
export function* chunkImportFiles(
  files: File[],
  start = 0,
): Generator<ImportFileChunk, void> {
  let chunkFiles: File[] = [];
  let chunkPaths: string[] = [];
  let chunkBytes = 0;
  let chunkStart = start;
  for (let index = start; index < files.length; index += 1) {
    const file = files[index];
    const wouldOverflow = chunkFiles.length > 0 && (
      chunkFiles.length >= IMPORT_SESSION_CHUNK_FILES
      || chunkBytes + file.size > IMPORT_SESSION_CHUNK_BYTES
    );
    if (wouldOverflow) {
      yield { files: chunkFiles, logicalPaths: chunkPaths, start: chunkStart };
      chunkFiles = [];
      chunkPaths = [];
      chunkBytes = 0;
      chunkStart = index;
    }
    chunkFiles.push(file);
    chunkPaths.push(importFileLogicalPath(file));
    chunkBytes += file.size;
  }
  if (chunkFiles.length) {
    yield { files: chunkFiles, logicalPaths: chunkPaths, start: chunkStart };
  }
}

interface CsvImportOptions extends RequestOptions {
  encoding?: string;
  destinationSheetId?: number;
  appendRequestKey?: string;
  columnMapping?: Record<string, string | null>;
}

type SampleProjectSeedWire =
  HttpContractSuccessResponse<'tenant.seed_sample_project.post'>;
type PreparedImportUrls = HttpContractSuccessResponse<'tenant.prepare_import_urls.post'>;
type ImportCsvWire = HttpContractSuccessResponse<'tenant.import_csv.post'>;
type ImportXlsxWire = HttpContractSuccessResponse<'tenant.import_xlsx.post'>;
type ImportFilesWire = HttpContractSuccessResponse<'tenant.import_files.post'>;
type ImportFollowTheMoneyWire =
  HttpContractSuccessResponse<'tenant.import_followthemoney.post'>;

type FollowTheMoneyRequest =
  HttpContractOperationMap['tenant.import_followthemoney.post']['request'];
/** The bulk-import routes are intentionally kept local until their generated
 * contract lands with the import surface. These are the stable wire shapes the
 * onboarding flow can use without interpreting a plan's decisions. */
export interface BulkImportQuestion {
  id: string;
  kind: string;
  default: string;
  logical_paths: string[];
}

export interface BulkImportProposedOutput {
  id: string;
  kind: string;
  sheet_name: string;
  logical_paths: string[];
}

export interface BulkImportPlan {
  plan_id: string;
  questions: BulkImportQuestion[];
  proposed_outputs: BulkImportProposedOutput[];
  message?: string;
}

export type PreparedImportFiles = HttpContractSuccessResponse<'tenant.prepare_import_files.post'>;

interface BulkImportCreatedOutput {
  rows?: number;
  search_preparation_batch_id?: string | null;
  id: string;
  kind: string;
  sheet_id: number;
  sheet_name: string;
  logical_paths: string[];
}

export interface BulkImportFailedOutput {
  logical_paths: string[];
  logical_paths_omitted: number;
  error: string;
}

interface BulkImportExecution {
  created: BulkImportCreatedOutput[];
  /** The server already bounds this deterministic first page of failures. */
  failed: BulkImportFailedOutput[];
  /** Number of further failures that did not fit in the bounded page. */
  failed_omitted: number;
  first_sheet_id: number | null;
  message: string;
  /** Optional on older servers; normalized to an empty list by the client. */
  warnings: string[];
}

interface SampleProjectSeedResult {
  ok: boolean;
  project_id: string;
  sheet_id: number;
  sheet_name: string;
  blank_column: string;
  rows: number;
}

export function createOnboardingImportsApi(projectId: string) {
  return {
    seedSampleProject(
      projectId: string,
      options: RequestOptions = {},
    ): Promise<SampleProjectSeedWire> {
      return httpContract(
        'tenant.seed_sample_project.post',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },

    importCsv(file: File, options: CsvImportOptions = {}): Promise<ImportCsvWire> {
      const body = new FormData();
      body.append('file', file);
      if (options.destinationSheetId) body.append('destination_sheet_id', String(options.destinationSheetId));
      if (options.appendRequestKey) body.append('append_request_key', options.appendRequestKey);
      if (options.columnMapping) body.append('column_mapping', JSON.stringify(options.columnMapping));
      const append = options.destinationSheetId != null;
      return httpContract(
        append ? 'tenant.append_csv.post' : 'tenant.import_csv.post',
        {
          pathParams: { pid: projectId },
          query: options.encoding ? { encoding: options.encoding } : {},
          body,
          signal: options.signal,
        },
      );
    },

    importXlsx(file: File, options: RequestOptions = {}): Promise<ImportXlsxWire> {
      const body = new FormData();
      body.append('file', file);
      return httpContract(
        'tenant.import_xlsx.post',
        { pathParams: { pid: projectId }, body, signal: options.signal },
      );
    },

    importFiles(files: File[], options: RequestOptions = {}): Promise<ImportFilesWire> {
      const body = new FormData();
      for (const file of files) body.append('files', file);
      return httpContract(
        'tenant.import_files.post',
        { pathParams: { pid: projectId }, body, signal: options.signal },
      );
    },

    importFollowTheMoney(
      file: File,
      datasetName?: string,
      options: RequestOptions = {},
    ): Promise<ImportFollowTheMoneyWire> {
      const body = new FormData();
      body.append('file', file);
      if (datasetName?.trim()) body.append('dataset_name', datasetName.trim());
      return httpContract(
        'tenant.import_followthemoney.post',
        {
          pathParams: { pid: projectId },
          body: body as FollowTheMoneyRequest,
          signal: options.signal,
        },
      );
    },

    planBulkImport(
      files: File[],
      logicalPaths: string[],
      expandArchive: boolean,
      options: RequestOptions = {},
    ): Promise<BulkImportPlan> {
      const body = new FormData();
      for (let index = 0; index < files.length; index += 1) {
        body.append('files', files[index]);
        body.append('logical_paths', logicalPaths[index] ?? '');
      }
      body.append('expand_archive', String(expandArchive));
      return httpContract(
        'tenant.import_bulk_plan.post',
        {
          pathParams: { pid: projectId },
          body,
          signal: options.signal,
        },
      );
    },

    executeBulkImport(
      planId: string,
      decisions: Record<string, string>,
      options: RequestOptions = {},
    ): Promise<BulkImportExecution> {
      return trackImportSubmission(projectId, { importKind: 'files', format: 'unknown' }, () => httpContract(
        'tenant.import_bulk_execute.post',
        {
          pathParams: { pid: projectId, plan_id: planId },
          body: { decisions },
          signal: options.signal,
        },
      ).then((execution): BulkImportExecution => {
        // Keep the public web type compatible with generated contracts that
        // predate the bounded omission counter, while exposing only display-
        // safe failure records to the import surface.
        const rawExecution = execution as typeof execution & { failed_omitted?: unknown };
        const failed = Array.isArray(execution.failed)
          ? execution.failed.flatMap((failure) => {
            if (
              !failure ||
              typeof failure.error !== 'string' ||
              !Array.isArray(failure.logical_paths) ||
              !failure.logical_paths.every((path) => typeof path === 'string')
            ) {
              return [];
            }
            const logical_paths_omitted = (
              typeof failure.logical_paths_omitted === 'number' &&
              Number.isSafeInteger(failure.logical_paths_omitted) &&
              failure.logical_paths_omitted > 0
            ) ? failure.logical_paths_omitted : 0;
            return [{
              logical_paths: failure.logical_paths,
              logical_paths_omitted,
              error: failure.error,
            }];
          })
          : [];
        const failed_omitted = (
          typeof rawExecution.failed_omitted === 'number' &&
          Number.isSafeInteger(rawExecution.failed_omitted) &&
          rawExecution.failed_omitted > 0
        ) ? rawExecution.failed_omitted : 0;
        return {
          ...execution,
          failed,
          failed_omitted,
        // Warning text is server-authored, ordinary text. Older servers did
        // not include this field, so keep their successful imports compatible.
        warnings: Array.isArray(execution.warnings)
          ? execution.warnings.filter((warning): warning is string => typeof warning === 'string')
          : [],
        };
      }), (result) => ({
        result: result.failed.length + result.failed_omitted > 0 ? result.created.length > 0 ? 'partial' : 'failed' : 'success',
        rows: result.created.every((output) => typeof output.rows === 'number')
          ? result.created.reduce((sum, output) => sum + (output.rows ?? 0), 0) : undefined,
      }));
    },
  };
}

export function seedSampleProject(projectId: string): Promise<SampleProjectSeedResult> {
  return createOnboardingImportsApi(projectId)
    .seedSampleProject(projectId) as Promise<SampleProjectSeedResult>;
}

export function importCsv(
  projectId: string,
  file: File,
  encoding?: string,
  destinationSheetId?: number,
  appendRequestKey?: string,
  columnMapping?: Record<string, string | null>,
): Promise<ImportCsvWire> {
  return createOnboardingImportsApi(projectId)
    .importCsv(file, { encoding, destinationSheetId, appendRequestKey, columnMapping });
}

export function importXlsx(
  projectId: string, file: File, destinationSheetId?: number,
  appendRequestKey?: string, columnMapping?: Record<string, string | null>,
): Promise<ImportXlsxWire> {
  if (!destinationSheetId) return createOnboardingImportsApi(projectId).importXlsx(file);
  const body = new FormData();
  body.append('file', file);
  body.append('destination_sheet_id', String(destinationSheetId));
  body.append('append_request_key', appendRequestKey ?? '');
  body.append('column_mapping', JSON.stringify(columnMapping ?? {}));
  return httpContract('tenant.append_xlsx.post', {
    pathParams: { pid: projectId }, body,
  }) as Promise<ImportXlsxWire>;
}

export function importFiles(projectId: string, files: File[]): Promise<ImportFilesWire> {
  return createOnboardingImportsApi(projectId).importFiles(files);
}

export function createImportSession(
  projectId: string,
  options: ImportSessionCreateOptions = {},
): Promise<ImportSessionStatus> {
  return httpContract('tenant.create_import_session.post', {
    pathParams: { pid: projectId },
    body: { sheet_name: options.sheetName?.trim() || 'files' },
    signal: options.signal,
  });
}

export function listImportSessions(
  projectId: string,
  options: RequestOptions = {},
): Promise<HttpImportSessionList> {
  return httpContract('tenant.list_import_sessions.get', {
    pathParams: { pid: projectId }, signal: options.signal,
  });
}

export function getImportSession(
  projectId: string,
  importRef: string,
  options: RequestOptions = {},
): Promise<ImportSessionStatus> {
  return httpContract('tenant.get_import_session.get', {
    pathParams: { pid: projectId, ref: importRef }, signal: options.signal,
  });
}

export function uploadImportSessionFiles(
  projectId: string,
  importRef: string,
  files: File[],
  options: ImportSessionUploadOptions,
): Promise<ImportSessionStatus> {
  if (!files.length || files.length > IMPORT_SESSION_CHUNK_FILES) {
    throw new TypeError('Import upload chunks must contain 1 to 256 files.');
  }
  if (files.length !== options.logicalPaths.length) {
    throw new TypeError('Import files and logical paths must stay aligned.');
  }
  const body = new FormData();
  for (let index = 0; index < files.length; index += 1) {
    body.append('files', files[index]);
    body.append('logical_paths', options.logicalPaths[index]);
  }
  body.append('batch_id', options.batchId);
  return httpContract('tenant.upload_import_session_files.post', {
    pathParams: { pid: projectId, ref: importRef }, body,
    signal: options.signal,
  });
}

export function sealImportSession(projectId: string, importRef: string, options: RequestOptions = {}) {
  return httpContract('tenant.seal_import_session.post', {
    pathParams: { pid: projectId, ref: importRef },
    signal: options.signal,
  });
}

export function cancelImportSession(projectId: string, importRef: string, options: RequestOptions = {}) {
  return httpContract('tenant.cancel_import_session.post', {
    pathParams: { pid: projectId, ref: importRef },
    signal: options.signal,
  });
}

export function resumeImportSession(projectId: string, importRef: string, options: RequestOptions = {}) {
  return httpContract('tenant.resume_import_session.post', {
    pathParams: { pid: projectId, ref: importRef },
    signal: options.signal,
  });
}

export function resolveImportSession(
  projectId: string,
  importRef: string,
  decision: 'keep' | 'remove',
  options: RequestOptions = {},
): Promise<ImportSessionStatus> {
  return httpContract('tenant.resolve_import_session.post', {
    pathParams: { pid: projectId, ref: importRef }, body: { decision },
    signal: options.signal,
  });
}

/** Prices reading an import's failed pages again; the plan token names exactly those pages. */
export function estimateImportPreparationRetry(
  projectId: string,
  importRef: string,
): Promise<ImportPreparationRetryEstimate> {
  return httpContract('tenant.estimate_import_preparation_retry.post', {
    pathParams: { pid: projectId, ref: importRef },
  }, (wire): ImportPreparationRetryEstimate => ({
    planToken: wire.plan_token,
    pages: wire.pages,
    estimate: runEstimateFromV1Wire(wire.estimate),
  }));
}

/** `confirmation` is the consented estimate's promise_set_hash, or null when
 * the estimate did not require consent. */
export function retryImportPreparation(
  projectId: string,
  importRef: string,
  planToken: string,
  confirmation: string | null,
): Promise<ImportSessionStatus> {
  return httpContract('tenant.retry_import_preparation.post', {
    pathParams: { pid: projectId, ref: importRef },
    body: { plan_token: planToken, confirmation },
  });
}

export async function uploadFilesToImportSession(
  projectId: string,
  session: ImportSessionStatus,
  files: File[],
  options: ImportSessionUploadSequenceOptions = {},
): Promise<ImportSessionStatus> {
  const { onStatus, ...requestOptions } = options;
  for (const chunk of chunkImportFiles(files, session.through)) {
    const end = chunk.start + chunk.files.length;
    const status = await uploadImportSessionFiles(projectId, session.import_ref, chunk.files, {
      ...requestOptions,
      logicalPaths: chunk.logicalPaths,
      // Stable across a re-selection of the same ordered file set. A response
      // lost after commit can safely retry this exact server batch marker.
      batchId: `${session.import_ref}:${chunk.start + 1}-${end}`,
    });
    onStatus?.(status);
  }
  const sealed = await sealImportSession(projectId, session.import_ref, requestOptions);
  onStatus?.(sealed);
  return sealed;
}

export function importFollowTheMoney(
  projectId: string,
  file: File,
  datasetName?: string,
): Promise<ImportFollowTheMoneyWire> {
  return createOnboardingImportsApi(projectId)
    .importFollowTheMoney(file, datasetName);
}

export function prepareImportFiles(
  projectId: string,
  files: File[],
  logicalPaths: string[],
  expandArchive: boolean,
  options: RequestOptions = {},
): Promise<PreparedImportFiles> {
  if (!files.length || files.length !== logicalPaths.length) {
    throw new TypeError('Prepared import files and logical paths must stay aligned.');
  }
  const body = new FormData();
  for (let index = 0; index < files.length; index += 1) {
    body.append('files', files[index]);
    body.append('logical_paths', logicalPaths[index]);
  }
  body.append('expand_archive', String(expandArchive));
  return httpContract('tenant.prepare_import_files.post', {
    pathParams: { pid: projectId },
    body,
    signal: options.signal,
  });
}

export function executeBulkImport(
  projectId: string,
  planId: string,
  decisions: Record<string, string>,
  options: RequestOptions = {},
): Promise<BulkImportExecution> {
  return createOnboardingImportsApi(projectId)
    .executeBulkImport(planId, decisions, options);
}

export function prepareImportUrls(
  projectId: string,
  urls: string[],
  options: RequestOptions = {},
): Promise<PreparedImportUrls> {
  return httpContract('tenant.prepare_import_urls.post', {
    pathParams: { pid: projectId }, body: { urls },
    signal: options.signal,
  });
}
