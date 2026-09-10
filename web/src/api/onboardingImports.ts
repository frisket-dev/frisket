import type {
  HttpContractOperationMap,
  HttpImportUpdatePreviewResponse,
} from '../generated/openHttpContracts';
import {
  ApiError,
  actionLaunchErrorFromContract,
} from './contractErrors';
import { httpContract, type HttpContractSuccessResponse } from './httpContract';

export interface OnboardingImportOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface CsvImportOptions extends OnboardingImportOptions {
  encoding?: string;
  destinationSheetId?: number;
  appendRequestKey?: string;
  columnMapping?: Record<string, string | null>;
}

export type SampleProjectSeedWire =
  HttpContractSuccessResponse<'tenant.seed_sample_project.post'>;
export type ImportPasteDraftWire =
  HttpContractSuccessResponse<'tenant.import_paste_draft.post'>;
export type ImportUrlsWire = HttpContractSuccessResponse<'tenant.import_urls.post'>;
export type ImportCsvWire = HttpContractSuccessResponse<'tenant.import_csv.post'>;
export type ImportCsvPreviewWire =
  HttpContractSuccessResponse<'tenant.import_csv_preview.post'>;
export type ImportXlsxWire = HttpContractSuccessResponse<'tenant.import_xlsx.post'>;
export type ImportPdfWire = HttpContractSuccessResponse<'tenant.import_pdf.post'>;
export type ImportFilesWire = HttpContractSuccessResponse<'tenant.import_files.post'>;
export type ImportFollowTheMoneyWire =
  HttpContractSuccessResponse<'tenant.import_followthemoney.post'>;

type PasteDraftRequest =
  HttpContractOperationMap['tenant.import_paste_draft.post']['request'];
type UrlsRequest = HttpContractOperationMap['tenant.import_urls.post']['request'];
type FollowTheMoneyRequest =
  HttpContractOperationMap['tenant.import_followthemoney.post']['request'];
type ContractErrorFactory = (status: number, payload: unknown) => Error;

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

export interface BulkImportCreatedOutput {
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

export interface BulkImportExecution {
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

export interface SampleProjectSeedResult {
  ok: boolean;
  project_id: string;
  sheet_id: number;
  sheet_name: string;
  blank_column: string;
  rows: number;
}

export interface ImportResult {
  sheet_id: number;
  rows: number;
  columns?: string[];
  pages?: number;
  downloaded?: number;
  failed?: number;
}

export type ImportCsvPreview = ImportCsvPreviewWire;

export interface ImportDraftColumn {
  key: string;
  name: string;
  type: string;
  include: boolean;
  format?: string | null;
  sample_values: unknown[];
}

export interface ImportDraft {
  schema_version: 'frisket.import_draft.v2';
  raw: string;
  draft_id: string;
  source_kind: 'paste';
  sheet_name: string;
  row_count: number;
  columns: ImportDraftColumn[];
  preview_rows: Array<Record<string, unknown>>;
  warnings: string[];
  source: Record<string, unknown>;
}

export interface ImportDraftMappingColumn {
  key: string;
  name: string;
  type: string;
  format?: string | null;
  include: boolean;
  updatePolicy?: 'match' | 'update';
}

export interface ImportDraftMapping {
  sheetName: string;
  destination?: { kind: 'new_sheet' } | { kind: 'existing_sheet'; sheetId: number };
  columns: ImportDraftMappingColumn[];
  update?: {
    keyColumns: string[];
    keepExistingOnBlank: boolean;
    confirmation: string;
  };
}

export type ImportUpdatePreview = HttpImportUpdatePreviewResponse;

export interface ImportUpdatePreviewInput {
  destinationSheetId: number;
  columns: ImportDraftMappingColumn[];
  keyColumns: string[];
  raw: string;
  draftId: string;
  keepExistingOnBlank: boolean;
}

export interface OnboardingImportsApi {
  seedSampleProject(
    projectId: string,
    options?: OnboardingImportOptions,
  ): Promise<SampleProjectSeedWire>;
  detectPastedRowsDraft(
    raw: string,
    options?: OnboardingImportOptions,
  ): Promise<ImportPasteDraftWire>;
  importUrls(
    urls: string[],
    options?: OnboardingImportOptions,
  ): Promise<ImportUrlsWire>;
  previewCsv(file: File, options?: CsvImportOptions): Promise<ImportCsvPreviewWire>;
  importCsv(file: File, options?: CsvImportOptions): Promise<ImportCsvWire>;
  importXlsx(file: File, options?: OnboardingImportOptions): Promise<ImportXlsxWire>;
  previewXlsx(file: File, options?: OnboardingImportOptions): Promise<ImportCsvPreviewWire>;
  importPdf(file: File, options?: OnboardingImportOptions): Promise<ImportPdfWire>;
  importFiles(files: File[], options?: OnboardingImportOptions): Promise<ImportFilesWire>;
  importFollowTheMoney(
    file: File,
    datasetName?: string,
    options?: OnboardingImportOptions,
  ): Promise<ImportFollowTheMoneyWire>;
  planBulkImport(
    files: File[],
    logicalPaths: string[],
    expandArchive: boolean,
    options?: OnboardingImportOptions,
  ): Promise<BulkImportPlan>;
  executeBulkImport(
    planId: string,
    decisions: Record<string, string>,
    options?: OnboardingImportOptions,
  ): Promise<BulkImportExecution>;
}

export function createOnboardingImportsApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): OnboardingImportsApi {
  return {
    seedSampleProject(projectId, options = {}) {
      return httpContract(
        'tenant.seed_sample_project.post',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    detectPastedRowsDraft(raw, options = {}) {
      return httpContract(
        'tenant.import_paste_draft.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: { raw } as PasteDraftRequest,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    importUrls(urls, options = {}) {
      return httpContract(
        'tenant.import_urls.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: { urls } as UrlsRequest,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    previewCsv(file, options = {}) {
      const body = new FormData();
      body.append('file', file);
      return httpContract(
        'tenant.import_csv_preview.post',
        {
          pathParams: { pid: projectId },
          query: options.encoding ? { encoding: options.encoding } : {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    importCsv(file, options = {}) {
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
          headers: options.headers,
          errorFactory,
        },
      );
    },

    importXlsx(file, options = {}) {
      const body = new FormData();
      body.append('file', file);
      return httpContract(
        'tenant.import_xlsx.post',
        { pathParams: { pid: projectId }, query: {}, body, signal: options.signal, headers: options.headers, errorFactory },
      );
    },

    previewXlsx(file, options = {}) {
      const body = new FormData();
      body.append('file', file);
      return httpContract('tenant.import_xlsx_preview.post', {
        pathParams: { pid: projectId }, query: {}, body,
        signal: options.signal, headers: options.headers, errorFactory,
      });
    },

    importPdf(file, options = {}) {
      const body = new FormData();
      body.append('file', file);
      return httpContract(
        'tenant.import_pdf.post',
        { pathParams: { pid: projectId }, query: {}, body, signal: options.signal, headers: options.headers, errorFactory },
      );
    },

    importFiles(files, options = {}) {
      const body = new FormData();
      for (const file of files) body.append('files', file);
      return httpContract(
        'tenant.import_files.post',
        { pathParams: { pid: projectId }, query: {}, body, signal: options.signal, headers: options.headers, errorFactory },
      );
    },

    importFollowTheMoney(file, datasetName, options = {}) {
      const body = new FormData();
      body.append('file', file);
      if (datasetName?.trim()) body.append('dataset_name', datasetName.trim());
      return httpContract(
        'tenant.import_followthemoney.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: body as FollowTheMoneyRequest,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    planBulkImport(files, logicalPaths, expandArchive, options = {}) {
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
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    executeBulkImport(planId, decisions, options = {}) {
      return httpContract(
        'tenant.import_bulk_execute.post',
        {
          pathParams: { pid: projectId, plan_id: planId },
          query: {},
          body: { decisions },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
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
      });
    },
  };
}

const MAX_PASTE_DRAFT_RAW_LENGTH = 2_000_000;

function isImportDraftRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasImportDraftProperty(record: Record<string, unknown>, property: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, property);
}

function unexpectedImportDraftResponse(): never {
  throw new ApiError(500, 'Unexpected import draft response');
}

function importDraftString(value: unknown): string {
  if (typeof value !== 'string') unexpectedImportDraftResponse();
  return value;
}

/** Maps the extensible producer response onto the browser's stable draft domain. */
export function normalizeImportDraft(wire: ImportPasteDraftWire, raw: string): ImportDraft {
  const draft: Record<string, unknown> = wire;
  if (!isImportDraftRecord(draft)) unexpectedImportDraftResponse();
  if (draft.schema_version !== 'frisket.import_draft.v2') unexpectedImportDraftResponse();
  if (draft.source_kind !== 'paste') unexpectedImportDraftResponse();

  const rowCount = draft.row_count;
  if (typeof rowCount !== 'number' || !Number.isFinite(rowCount) || !Number.isInteger(rowCount)) {
    unexpectedImportDraftResponse();
  }
  if (!Array.isArray(draft.columns) || !Array.isArray(draft.preview_rows) || !Array.isArray(draft.warnings)) {
    unexpectedImportDraftResponse();
  }

  const columns: ImportDraftColumn[] = draft.columns.map((value) => {
    if (!isImportDraftRecord(value)) unexpectedImportDraftResponse();
    const hasFormat = hasImportDraftProperty(value, 'format');
    const format = value.format;
    if (hasFormat && format !== null && typeof format !== 'string') unexpectedImportDraftResponse();
    if (!hasImportDraftProperty(value, 'sample_values') || !Array.isArray(value.sample_values)) {
      unexpectedImportDraftResponse();
    }
    if (typeof value.include !== 'boolean') unexpectedImportDraftResponse();
    const extensions = { ...value };
    delete extensions.key;
    delete extensions.name;
    delete extensions.type;
    delete extensions.include;
    delete extensions.format;
    delete extensions.sample_values;
    return {
      ...extensions,
      key: importDraftString(value.key),
      name: importDraftString(value.name),
      type: importDraftString(value.type),
      include: value.include,
      ...(hasFormat ? { format: format as string | null } : {}),
      sample_values: value.sample_values,
    };
  });

  const preview_rows: Array<Record<string, unknown>> = draft.preview_rows.map((value) => {
    if (!isImportDraftRecord(value)) unexpectedImportDraftResponse();
    return value;
  });
  const warnings: string[] = draft.warnings.map(importDraftString);
  const source = draft.source;
  if (
    !isImportDraftRecord(source)
    || typeof source.kind !== 'string'
    || typeof source.label !== 'string'
    || typeof source.fingerprint !== 'string'
    || typeof source.line_count !== 'number'
  ) {
    unexpectedImportDraftResponse();
  }

  return {
    ...draft,
    schema_version: 'frisket.import_draft.v2',
    draft_id: importDraftString(draft.draft_id),
    source_kind: 'paste',
    sheet_name: importDraftString(draft.sheet_name),
    row_count: rowCount,
    raw,
    columns,
    preview_rows,
    warnings,
    source,
  };
}

function reviewedDraftColumns(columns: ImportDraftMappingColumn[]) {
  return columns.map((column) => ({
    source_name: column.key,
    name: column.include ? column.name.trim() : null,
    type: column.type,
    ...(column.format ? { format: column.format } : {}),
  }));
}

export async function submitImportRowsDraft(
  projectId: string,
  draft: ImportDraft,
  mapping: ImportDraftMapping,
): Promise<ImportResult> {
  return httpContract('tenant.import_paste_confirm.post', {
    pathParams: { pid: projectId },
    query: {},
    body: {
      raw: draft.raw,
      draft_id: draft.draft_id,
      sheet_name: mapping.sheetName.trim() || draft.sheet_name,
      columns: reviewedDraftColumns(mapping.columns),
      ...(mapping.destination?.kind === 'existing_sheet'
        ? { destination_sheet_id: mapping.destination.sheetId } : {}),
      ...(mapping.update ? {
        key_columns: mapping.update.keyColumns,
        keep_existing_on_blank: mapping.update.keepExistingOnBlank,
        confirmation: mapping.update.confirmation,
      } : {}),
    },
    errorFactory: actionLaunchErrorFromContract,
  });
}

function updatePreviewError(status: number, payload: unknown): Error {
  return actionLaunchErrorFromContract(status, payload);
}

export async function previewImportRowUpdates(
  projectId: string,
  input: ImportUpdatePreviewInput,
  options: OnboardingImportOptions = {},
): Promise<ImportUpdatePreview> {
  const body = {
    raw: input.raw,
    draft_id: input.draftId,
    destination_sheet_id: input.destinationSheetId,
    columns: reviewedDraftColumns(input.columns),
    key_columns: input.keyColumns,
    keep_existing_on_blank: input.keepExistingOnBlank,
  };
  const payload: unknown = await httpContract('tenant.import_update_preview.post', {
    pathParams: { pid: projectId }, query: {}, body,
    signal: options.signal, headers: options.headers, errorFactory: updatePreviewError,
  });
  if (!payload || typeof payload !== 'object') {
    throw new ApiError(500, 'Unexpected update preview response');
  }
  const preview = payload as Partial<ImportUpdatePreview>;
  const counts = [
    preview.matched, preview.unmatched, preview.blank_keys, preview.ambiguous,
    preview.changed_cells, preview.cleared_cells,
  ];
  if (
    counts.some((count) => typeof count !== 'number' || !Number.isSafeInteger(count) || count < 0)
    || !Array.isArray(preview.samples)
    || typeof preview.confirmation !== 'string'
    || !preview.confirmation
  ) {
    throw new ApiError(500, 'Unexpected update preview response');
  }
  return preview as ImportUpdatePreview;
}

export async function previewImportedFileUpdates(
  projectId: string,
  file: File,
  format: 'csv' | 'xlsx',
  input: {
    destinationSheetId: number;
    columnMapping: Record<string, string | null>;
    keyColumns: string[];
    keepExistingOnBlank: boolean;
    encoding?: string;
  },
  options: OnboardingImportOptions = {},
): Promise<ImportUpdatePreview> {
  const body = new FormData();
  body.append('file', file);
  body.append('destination_sheet_id', String(input.destinationSheetId));
  body.append('column_mapping', JSON.stringify(input.columnMapping));
  body.append('key_columns', JSON.stringify(input.keyColumns));
  body.append('keep_existing_on_blank', String(input.keepExistingOnBlank));
  const payload: unknown = await httpContract(
    format === 'xlsx' ? 'tenant.import_xlsx_update_preview.post' : 'tenant.import_csv_update_preview.post',
    {
      pathParams: { pid: projectId },
      query: input.encoding ? { encoding: input.encoding } : {},
      body,
      signal: options.signal,
      errorFactory: updatePreviewError,
    },
  );
  return payload as ImportUpdatePreview;
}

export async function updateRowsFromImportedFile(
  projectId: string,
  file: File,
  format: 'csv' | 'xlsx',
  input: {
    destinationSheetId: number;
    columnMapping: Record<string, string | null>;
    keyColumns: string[];
    keepExistingOnBlank: boolean;
    confirmation: string;
    requestKey: string;
    encoding?: string;
  },
): Promise<{ sheet_id: number; rows: number; columns: string[] }> {
  const body = new FormData();
  body.append('file', file);
  body.append('destination_sheet_id', String(input.destinationSheetId));
  body.append('column_mapping', JSON.stringify(input.columnMapping));
  body.append('key_columns', JSON.stringify(input.keyColumns));
  body.append('keep_existing_on_blank', String(input.keepExistingOnBlank));
  body.append('confirmation', input.confirmation);
  body.append('update_request_key', input.requestKey);
  return httpContract(
    format === 'xlsx' ? 'tenant.update_xlsx.post' : 'tenant.update_csv.post',
    {
      pathParams: { pid: projectId },
      query: input.encoding ? { encoding: input.encoding } : {},
      body,
      errorFactory: updatePreviewError,
    },
  );
}

export function seedSampleProject(projectId: string): Promise<SampleProjectSeedResult> {
  return createOnboardingImportsApi(actionLaunchErrorFromContract, projectId)
    .seedSampleProject(projectId) as Promise<SampleProjectSeedResult>;
}

export async function detectPastedRowsDraft(projectId: string, raw: string): Promise<ImportDraft> {
  if (raw.length > MAX_PASTE_DRAFT_RAW_LENGTH) {
    throw new ApiError(413, 'Paste is too large; limit is 2,000,000 characters.');
  }
  return normalizeImportDraft(
    await createOnboardingImportsApi(actionLaunchErrorFromContract, projectId)
      .detectPastedRowsDraft(raw),
    raw,
  );
}

export function previewCsv(
  projectId: string,
  file: File,
  encoding?: string,
  options: OnboardingImportOptions = {},
): Promise<ImportCsvPreview> {
  return createOnboardingImportsApi(actionLaunchErrorFromContract, projectId)
    .previewCsv(file, { ...options, encoding });
}

export function importCsv(
  projectId: string,
  file: File,
  encoding?: string,
  destinationSheetId?: number,
  appendRequestKey?: string,
  columnMapping?: Record<string, string | null>,
): Promise<ImportCsvWire> {
  return createOnboardingImportsApi(actionLaunchErrorFromContract, projectId)
    .importCsv(file, { encoding, destinationSheetId, appendRequestKey, columnMapping });
}

export function importXlsx(
  projectId: string, file: File, destinationSheetId?: number,
  appendRequestKey?: string, columnMapping?: Record<string, string | null>,
): Promise<ImportXlsxWire> {
  if (!destinationSheetId) return createOnboardingImportsApi(actionLaunchErrorFromContract, projectId).importXlsx(file);
  const body = new FormData();
  body.append('file', file);
  body.append('destination_sheet_id', String(destinationSheetId));
  body.append('append_request_key', appendRequestKey ?? '');
  body.append('column_mapping', JSON.stringify(columnMapping ?? {}));
  return httpContract('tenant.append_xlsx.post', {
    pathParams: { pid: projectId }, query: {}, body, errorFactory: actionLaunchErrorFromContract,
  }) as Promise<ImportXlsxWire>;
}

export function previewXlsx(projectId: string, file: File): Promise<ImportCsvPreview> {
  return createOnboardingImportsApi(actionLaunchErrorFromContract, projectId).previewXlsx(file);
}

export function importPdf(projectId: string, file: File): Promise<ImportPdfWire> {
  return createOnboardingImportsApi(actionLaunchErrorFromContract, projectId).importPdf(file);
}

export function importFiles(projectId: string, files: File[]): Promise<ImportFilesWire> {
  return createOnboardingImportsApi(actionLaunchErrorFromContract, projectId).importFiles(files);
}

export function importFollowTheMoney(
  projectId: string,
  file: File,
  datasetName?: string,
): Promise<ImportFollowTheMoneyWire> {
  return createOnboardingImportsApi(actionLaunchErrorFromContract, projectId)
    .importFollowTheMoney(file, datasetName);
}

export function planBulkImport(
  projectId: string,
  files: File[],
  logicalPaths: string[],
  expandArchive: boolean,
  options: OnboardingImportOptions = {},
): Promise<BulkImportPlan> {
  return createOnboardingImportsApi(actionLaunchErrorFromContract, projectId)
    .planBulkImport(files, logicalPaths, expandArchive, options);
}

export function executeBulkImport(
  projectId: string,
  planId: string,
  decisions: Record<string, string>,
  options: OnboardingImportOptions = {},
): Promise<BulkImportExecution> {
  return createOnboardingImportsApi(actionLaunchErrorFromContract, projectId)
    .executeBulkImport(planId, decisions, options);
}

export function importUrls(projectId: string, urls: string[]): Promise<ImportUrlsWire> {
  const cleanedUrls = urls.flatMap((url) => {
    const trimmed = url.trim();
    return trimmed ? [trimmed] : [];
  });
  return createOnboardingImportsApi(actionLaunchErrorFromContract, projectId)
    .importUrls(cleanedUrls);
}
