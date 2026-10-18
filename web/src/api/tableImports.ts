import type {
  HttpTableImportColumnsRequest,
  HttpTableImportCommitResponse,
  HttpTableImportDestinationRequest,
  HttpTableImportDraft,
  HttpTableImportSettingsRequest,
} from '../generated/openHttpContracts';
import { httpContract } from './httpContract';

export const TABLE_IMPORT_LARGE_PASTE_WARNING_BYTES = 20 * 1024 * 1024;

export type TableImportDraft = HttpTableImportDraft;
export type TableImportCommitResult = HttpTableImportCommitResponse;
export type TableImportSettings = NonNullable<TableImportDraft['settings']>;
export type TableImportRequestedSettings = TableImportSettings['requested'];
export type TableImportResolvedSettings = TableImportSettings['resolved'];
export type TableImportPreview = NonNullable<TableImportDraft['preview']>;
export type TableImportColumn = TableImportDraft['columns'][number];
export type TableImportColumnPatch = HttpTableImportColumnsRequest['columns'][number];
export type TableImportTargetType = TableImportColumnPatch['target_type'];
export type TableImportDateOrder = NonNullable<TableImportColumnPatch['date_order']>;
export type TableImportDestinationConfig = HttpTableImportDestinationRequest['destination'];
export type TableImportMappingRequest = HttpTableImportDestinationRequest['mappings'][number];
export type TableImportDestinationTarget = TableImportMappingRequest['target'];
export type TableImportDestinationSnapshot = NonNullable<TableImportDraft['destination']>;

export interface TableImportRequestOptions {
  signal?: AbortSignal;
  fetch?: typeof globalThis.fetch;
}

export interface TableImportsApi {
  createFromFile(projectId: string, file: File, options?: TableImportRequestOptions): Promise<TableImportDraft>;
  createFromPaste(projectId: string, file: File, largePasteConfirmed: boolean, options?: TableImportRequestOptions): Promise<TableImportDraft>;
  get(projectId: string, draftId: string, options?: TableImportRequestOptions): Promise<TableImportDraft>;
  unlock(projectId: string, draftId: string, password: string, options?: TableImportRequestOptions): Promise<TableImportDraft>;
  updateSettings(projectId: string, draftId: string, expectedRevision: number, settings: TableImportRequestedSettings, options?: TableImportRequestOptions): Promise<TableImportDraft>;
  updateColumns(projectId: string, draftId: string, expectedRevision: number, columns: TableImportColumnPatch[], options?: TableImportRequestOptions): Promise<TableImportDraft>;
  updateDestination(projectId: string, draftId: string, expectedRevision: number, destination: TableImportDestinationConfig, mappings: TableImportMappingRequest[], dedup: HttpTableImportDestinationRequest['dedup'], options?: TableImportRequestOptions): Promise<TableImportDraft>;
  commit(projectId: string, draftId: string, expectedRevision: number, idempotencyKey: string, options?: TableImportRequestOptions): Promise<TableImportCommitResult>;
  discard(projectId: string, draftId: string, options?: TableImportRequestOptions): Promise<void>;
}

export class TableImportResponseError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    const detail = typeof payload === 'object' && payload !== null && 'detail' in payload
      ? (payload as { detail: unknown }).detail : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail && typeof detail.message === 'string'
      ? detail.message : typeof detail === 'string' ? detail : `Request failed (HTTP ${status})`;
    super(message);
    this.name = 'TableImportResponseError';
    this.status = status;
    this.code = typeof detail === 'object' && detail !== null && 'code' in detail && typeof detail.code === 'string'
      ? detail.code : null;
    this.payload = payload;
  }
}

function requestOptions(options: TableImportRequestOptions) {
  return {
    signal: options.signal,
    fetch: options.fetch,
    errorFactory: (status: number, payload: unknown) => new TableImportResponseError(status, payload),
  };
}

export function createTableImportsApi(): TableImportsApi {
  return {
    createFromFile(projectId, file, options = {}) {
      const body = new FormData();
      body.append('file', file);
      return httpContract('tenant.create_table_import_from_file.post', {
        pathParams: { pid: projectId }, query: {}, body, ...requestOptions(options),
      });
    },
    createFromPaste(projectId, file, largePasteConfirmed, options = {}) {
      const body = new FormData();
      body.append('file', file);
      body.append('large_paste_confirmed', String(largePasteConfirmed));
      return httpContract('tenant.create_table_import_from_paste.post', {
        pathParams: { pid: projectId }, query: {}, body, ...requestOptions(options),
      });
    },
    get(projectId, draftId, options = {}) {
      return httpContract('tenant.get_table_import.get', {
        pathParams: { pid: projectId, draft_id: draftId }, query: {}, ...requestOptions(options),
      });
    },
    unlock(projectId, draftId, password, options = {}) {
      return httpContract('tenant.unlock_table_import.post', {
        pathParams: { pid: projectId, draft_id: draftId }, query: {}, body: { password }, ...requestOptions(options),
      });
    },
    updateSettings(projectId, draftId, expectedRevision, settings, options = {}) {
      const body: HttpTableImportSettingsRequest = { expected_revision: expectedRevision, settings };
      return httpContract('tenant.update_table_import_settings.patch', {
        pathParams: { pid: projectId, draft_id: draftId }, query: {}, body, ...requestOptions(options),
      });
    },
    updateColumns(projectId, draftId, expectedRevision, columns, options = {}) {
      const body: HttpTableImportColumnsRequest = { expected_revision: expectedRevision, columns };
      return httpContract('tenant.update_table_import_columns.patch', {
        pathParams: { pid: projectId, draft_id: draftId }, query: {}, body, ...requestOptions(options),
      });
    },
    updateDestination(projectId, draftId, expectedRevision, destination, mappings, dedup, options = {}) {
      const body: HttpTableImportDestinationRequest = { expected_revision: expectedRevision, destination, mappings, dedup };
      return httpContract('tenant.update_table_import_destination.patch', {
        pathParams: { pid: projectId, draft_id: draftId }, query: {}, body, ...requestOptions(options),
      });
    },
    commit(projectId, draftId, expectedRevision, idempotencyKey, options = {}) {
      return httpContract('tenant.commit_table_import.post', {
        pathParams: { pid: projectId, draft_id: draftId }, query: {},
        body: { expected_revision: expectedRevision, idempotency_key: idempotencyKey },
        ...requestOptions(options),
      });
    },
    discard(projectId, draftId, options = {}) {
      return httpContract('tenant.discard_table_import.delete', {
        pathParams: { pid: projectId, draft_id: draftId }, query: {}, ...requestOptions(options),
      });
    },
  };
}

export const tableImportsApi = createTableImportsApi();
