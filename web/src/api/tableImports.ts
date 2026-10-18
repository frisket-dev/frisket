export const TABLE_IMPORT_LARGE_PASTE_WARNING_BYTES = 20 * 1024 * 1024;

export type TableImportFormat = 'delimited' | 'xlsx' | 'xls' | 'ods';
export type TableImportTargetType = 'text' | 'integer' | 'number' | 'date' | 'datetime' | 'boolean' | 'omit';
export type TableImportDateOrder = 'month_first' | 'day_first';

export interface TableImportRequestedSettings {
  delimiter: string | null;
  encoding: string | null;
  decimal_separator: 'auto' | '.' | ',';
  thousands_separator: 'auto' | ',' | '.' | ' ' | 'none';
  worksheet: string | null;
  skip_rows_start: number;
  first_row_is_header: boolean;
  skip_rows_end: number;
}

export interface TableImportResolvedSettings {
  kind: TableImportFormat;
  delimiter: string | null;
  encoding: string | null;
  decimal_separator: '.' | ',';
  thousands_separator: string | null;
  worksheet: string | null;
  skip_rows_start: number;
  first_row_is_header: boolean;
  skip_rows_end: number;
}

export interface TableImportPreviewColumn { index: number; name: string; original_name: string | null; name_status: 'original' | 'duplicate' | 'missing' }
export interface TableImportPreviewIssue { code: 'extra_values' | 'missing_values' | 'malformed_record' | 'unreadable_cell'; message: string; original_values: string[] }
export interface TableImportPreviewRow {
  role: 'skipped_start' | 'header' | 'data' | 'blank' | 'skipped_end';
  record_ordinal: number;
  physical_line_start: number | null;
  physical_line_end: number | null;
  cells: string[];
  hidden_cell_count: number;
  issues: TableImportPreviewIssue[];
}
export interface TableImportPreview {
  columns: TableImportPreviewColumn[];
  head: TableImportPreviewRow[];
  tail: TableImportPreviewRow[];
  rows_seen: number;
  omitted_rows: number | null;
  complete: boolean;
  tail_pending: boolean;
  expected_values: number;
  notice: {
    kind: 'extra_values' | 'unreadable_characters' | 'one_column' | 'mostly_wrong_width' | 'some_wrong_width' | 'consistent' | 'counting';
    count: number | null;
    common_values: number | null;
    expected_values: number | null;
    encoding: string | null;
  };
}

export interface TableImportColumnPatch { source_index: number; included: boolean; target_type: TableImportTargetType; date_order: TableImportDateOrder | null }
export interface TableImportColumn extends TableImportColumnPatch {
  name: string;
  original_name: string | null;
  name_status: 'original' | 'duplicate' | 'missing';
  suggested_type: TableImportTargetType;
  ambiguous_date_order: boolean;
  needs_review: Array<'leading_zeros' | 'long_integer' | 'ambiguous_date_order' | 'invalid_values' | 'time_dropped'>;
  samples: Array<{ original: string; value: string | number | boolean | null; display: string; outcome: 'unchanged' | 'changed' | 'blank' | 'invalid' }>;
  conversion_counts: { rows_checked: number; valid: number; blank: number; changed: number; invalid: number; leading_zero_loss: number; time_dropped: number };
  stats_complete: boolean;
}

export type TableImportDestinationConfig = { kind: 'new'; sheet_name: string } | { kind: 'existing'; sheet_id: number };
export type TableImportDestinationTarget = { kind: 'existing'; column_id: string } | { kind: 'new'; name: string };
export interface TableImportDestinationSnapshot {
  configuration: TableImportDestinationConfig;
  mappings: Array<{ source_index: number; source_name: string; source_type: TableImportTargetType; target: TableImportDestinationTarget; match: 'matched' | 'similar' | 'new' | 'type_clash'; detail: string | null }>;
  dedup: { enabled: boolean; key_source_index: number | null };
  comparison: { state: 'idle' | 'running' | 'ready' | 'error'; matched_columns: number; new_columns: number; type_clashes: number; duplicate_rows: number | null; import_rows: number | null; complete: boolean; error: string | null };
}

export interface TableImportCommitResult {
  schema_version: string;
  draft_id: string;
  commit_id: string;
  status: 'accepted';
  import_ref: string;
  sheet_id: number;
  sheet_name: string;
  selected_total: number;
}

export interface TableImportDraft {
  schema_version: 'frisket.table_import.v1';
  id: string;
  draft_id: string;
  state: 'password_required' | 'analyzing' | 'ready' | 'committing' | 'committed' | 'error';
  revision: number;
  created_at: string;
  updated_at: string;
  source: { input_kind: 'file' | 'paste'; filename: string; size: number; sha256: string; format: TableImportFormat | null; sheets: Array<{ index: number; name: string; hidden: boolean; empty: boolean }>; can_rerun_workbook: boolean };
  settings: { revision: number; requested: TableImportRequestedSettings; resolved: TableImportResolvedSettings };
  analysis: { state: 'idle' | 'running' | 'complete' | 'error'; job_id: number | null; scanned_rows: number; estimated_rows: number | null; exact_rows: number | null; complete: boolean; issue_counts: { extra_values: number; missing_values: number; malformed_records: number; unreadable_cells: number }; error: string | null };
  preview: TableImportPreview | null;
  columns: TableImportColumn[];
  destination: TableImportDestinationSnapshot | null;
  can_commit: boolean;
  commit_blocked_reason: 'password_required' | 'analyzing' | 'empty' | 'destination' | 'error' | null;
  commit: TableImportCommitResult | null;
}

export interface TableImportRequestOptions { signal?: AbortSignal; fetch?: typeof globalThis.fetch }

export interface TableImportsApi {
  createFromFile(projectId: string, file: File, options?: TableImportRequestOptions): Promise<TableImportDraft>;
  createFromPaste(projectId: string, file: File, largePasteConfirmed: boolean, options?: TableImportRequestOptions): Promise<TableImportDraft>;
  get(projectId: string, draftId: string, options?: TableImportRequestOptions): Promise<TableImportDraft>;
  unlock(projectId: string, draftId: string, password: string, options?: TableImportRequestOptions): Promise<TableImportDraft>;
  updateSettings(projectId: string, draftId: string, expectedRevision: number, settings: TableImportRequestedSettings, options?: TableImportRequestOptions): Promise<TableImportDraft>;
  updateColumns(projectId: string, draftId: string, expectedRevision: number, columns: TableImportColumnPatch[], options?: TableImportRequestOptions): Promise<TableImportDraft>;
  updateDestination(projectId: string, draftId: string, expectedRevision: number, destination: TableImportDestinationConfig, mappings: TableImportDestinationSnapshot['mappings'], dedup: TableImportDestinationSnapshot['dedup'], options?: TableImportRequestOptions): Promise<TableImportDraft>;
  commit(projectId: string, draftId: string, expectedRevision: number, idempotencyKey: string, options?: TableImportRequestOptions): Promise<TableImportCommitResult>;
  discard(projectId: string, draftId: string, options?: TableImportRequestOptions): Promise<void>;
}

export class TableImportResponseError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly payload: unknown;
  constructor(status: number, payload: unknown) {
    const detail = typeof payload === 'object' && payload !== null && 'detail' in payload ? (payload as { detail: unknown }).detail : null;
    const message = typeof detail === 'object' && detail !== null && 'message' in detail && typeof detail.message === 'string' ? detail.message : typeof detail === 'string' ? detail : `Request failed (HTTP ${status})`;
    super(message);
    this.name = 'TableImportResponseError';
    this.status = status;
    this.code = typeof detail === 'object' && detail !== null && 'code' in detail && typeof detail.code === 'string' ? detail.code : null;
    this.payload = payload;
  }
}

const root = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/import/table-imports`;

async function request<T>(path: string, init: RequestInit, options: TableImportRequestOptions): Promise<T> {
  const response = await (options.fetch ?? globalThis.fetch)(path, { ...init, signal: options.signal });
  if (response.status === 204) return undefined as T;
  let payload: unknown;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) throw new TableImportResponseError(response.status, payload);
  return payload as T;
}

function json(body: unknown): RequestInit { return { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }; }

export function createTableImportsApi(): TableImportsApi {
  return {
    createFromFile(projectId, file, options = {}) { const body = new FormData(); body.append('file', file); return request(`${root(projectId)}/file`, { method: 'POST', body }, options); },
    createFromPaste(projectId, file, largePasteConfirmed, options = {}) { const body = new FormData(); body.append('file', file); body.append('large_paste_confirmed', String(largePasteConfirmed)); return request(`${root(projectId)}/paste`, { method: 'POST', body }, options); },
    get(projectId, draftId, options = {}) { return request(`${root(projectId)}/${encodeURIComponent(draftId)}`, { method: 'GET' }, options); },
    unlock(projectId, draftId, password, options = {}) { return request(`${root(projectId)}/${encodeURIComponent(draftId)}/unlock`, { method: 'POST', ...json({ password }) }, options); },
    updateSettings(projectId, draftId, expectedRevision, settings, options = {}) { return request(`${root(projectId)}/${encodeURIComponent(draftId)}/settings`, { method: 'PATCH', ...json({ expected_revision: expectedRevision, settings }) }, options); },
    updateColumns(projectId, draftId, expectedRevision, columns, options = {}) { return request(`${root(projectId)}/${encodeURIComponent(draftId)}/columns`, { method: 'PATCH', ...json({ expected_revision: expectedRevision, columns }) }, options); },
    updateDestination(projectId, draftId, expectedRevision, destination, mappings, dedup, options = {}) { return request(`${root(projectId)}/${encodeURIComponent(draftId)}/destination`, { method: 'PATCH', ...json({ expected_revision: expectedRevision, destination, mappings: mappings.map(({ source_index, target }) => ({ source_index, target })), dedup }) }, options); },
    commit(projectId, draftId, expectedRevision, idempotencyKey, options = {}) { return request(`${root(projectId)}/${encodeURIComponent(draftId)}/commit`, { method: 'POST', ...json({ expected_revision: expectedRevision, idempotency_key: idempotencyKey }) }, options); },
    discard(projectId, draftId, options = {}) { return request(`${root(projectId)}/${encodeURIComponent(draftId)}`, { method: 'DELETE' }, options); },
  };
}

export const tableImportsApi = createTableImportsApi();
