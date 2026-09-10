import { ACTION_PLACEMENTS } from '../actions/model';
import { httpContract } from '../api/httpContract';

export type TelemetryPreference = 'unanswered' | 'enabled' | 'disabled';

export type DurationBucket = 'lt_1s' | '1_10s' | '10_60s' | '1_10m' | 'gte_10m' | 'unknown';
export type ByteBucket = 'zero' | 'lt_1mb' | '1_100mb' | '100mb_1gb' | 'gte_1gb' | 'unknown';
export type RowBucket = 'zero' | '1_999' | '1k_99k' | '100k_999k' | 'gte_1m' | 'unknown';
export type ColumnBucket = 'zero' | '1_5' | '6_20' | '21_100' | 'gte_101' | 'unknown';
export type FailureCategory = 'none' | 'invalid_input' | 'unsupported_format' | 'permission'
  | 'missing_dependency' | 'missing_credential' | 'network' | 'provider'
  | 'rate_limited' | 'timeout' | 'empty_output' | 'invalid_output' | 'conflict'
  | 'internal' | 'unknown';

export type ProductTelemetryEvent =
  | { type: 'App.opened'; properties: { platform: 'linux' | 'macos' | 'windows' | 'ios' | 'android' | 'other' } }
  | { type: 'Project.created'; properties: { creationKind: 'blank' | 'sample'; requestDuration: DurationBucket } }
  | { type: 'Project.createFailed'; properties: { creationKind: 'blank' | 'sample'; requestDuration: DurationBucket; failureCategory: FailureCategory } }
  | { type: 'Project.opened'; properties: Record<string, never> }
  | { type: 'Import.started'; properties: { importKind: 'file' | 'files' | 'paste' | 'url'; format: ImportFormat; bytes: ByteBucket } }
  | { type: 'Import.finished'; properties: { importKind: 'file' | 'files' | 'paste' | 'url'; format: ImportFormat; bytes: ByteBucket; rows: RowBucket; columns: ColumnBucket; requestDuration: DurationBucket; result: 'success' | 'partial' | 'rejected' | 'failed'; failureCategory: FailureCategory } }
  | { type: 'Source.created'; properties: { sourceKind: SourceKind; requestDuration: DurationBucket; result: 'success' | 'rejected' | 'failed'; failureCategory: FailureCategory } }
  | { type: 'Action.opened'; properties: { action: string } }
  | { type: 'Action.runSubmitted'; properties: { action: string; attemptKind: AttemptKind; rows: RowBucket; columns: ColumnBucket; requestDuration: DurationBucket; result: 'success' | 'queued' | 'needs_confirmation' | 'rejected' | 'failed' | 'cancelled'; failureCategory: FailureCategory } }
  | { type: 'Action.runObserved'; properties: { action: string; attemptKind: AttemptKind; observedDuration: DurationBucket; result: 'success' | 'partial' | 'failed' | 'cancelled' | 'stalled' | 'orphaned' | 'no_live_worker'; failureCategory: FailureCategory } }
  | { type: 'Export.requested'; properties: { exportKind: ExportKind } };

export type ImportFormat = 'csv' | 'xlsx' | 'json' | 'jsonl' | 'parquet' | 'pdf'
  | 'image' | 'audio' | 'video' | 'html' | 'text' | 'mixed' | 'other' | 'unknown';
export type SourceKind = 'rss' | 'youtube_playlist' | 'youtube_channel' | 'api' | 'courtlistener' | 'other';
export type AttemptKind = 'initial' | 'retry' | 'resume' | 'backfill';
export type ExportKind = 'sheet_csv' | 'sheet_xlsx' | 'project_bundle'
  | 'project_bundle_without_media' | 'project_database' | 'work_log';

type StoredTelemetry = {
  schema_version: 1;
  state: 'enabled' | 'disabled';
  utc_month?: string;
  monthly_id?: string;
};

const STORAGE_KEY = 'frisket:product-telemetry:v1';
let available = false;
let appOpenedSent = false;

function monthNow(): string {
  const now = new Date();
  return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}`;
}

function randomId(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
}

function readStored(): StoredTelemetry | null {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null') as unknown;
    if (!value || typeof value !== 'object') return null;
    const record = value as Partial<StoredTelemetry>;
    if (record.schema_version !== 1 || (record.state !== 'enabled' && record.state !== 'disabled')) return null;
    return record as StoredTelemetry;
  } catch {
    return null;
  }
}

function writeStored(value: StoredTelemetry): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  } catch {
    // Blocked storage disables telemetry without affecting the product.
  }
}

export function telemetryPreference(): TelemetryPreference {
  return readStored()?.state ?? 'unanswered';
}

export function setTelemetryPreference(enabled: boolean): void {
  writeStored({ schema_version: 1, state: enabled ? 'enabled' : 'disabled' });
  if (!enabled) appOpenedSent = false;
}

export function clearTelemetryPseudonym(): void {
  const stored = readStored();
  if (stored) writeStored({ schema_version: 1, state: stored.state });
  appOpenedSent = false;
}

function enabledIdentity(): string | null {
  const stored = readStored();
  if (stored?.state !== 'enabled') return null;
  const month = monthNow();
  if (stored.utc_month === month && /^[0-9a-f]{64}$/.test(stored.monthly_id ?? '')) {
    return stored.monthly_id ?? null;
  }
  const monthlyId = randomId();
  writeStored({ schema_version: 1, state: 'enabled', utc_month: month, monthly_id: monthlyId });
  return monthlyId;
}

export function configureProductTelemetry(isAvailable: boolean): void {
  available = isAvailable;
}

export function sendProductTelemetry(event: ProductTelemetryEvent, projectId?: string): void {
  if (!available) return;
  const monthlyId = enabledIdentity();
  if (!monthlyId) return;
  const body = { monthly_id: monthlyId, ...event };
  const request = projectId
    ? httpContract('tenant.product_telemetry_project.post', {
        pathParams: { pid: projectId },
        query: {},
        body,
        credentials: 'same-origin',
        keepalive: true,
      })
    : httpContract('tenant.product_telemetry_installation.post', {
        pathParams: {},
        query: {},
        body,
        credentials: 'same-origin',
        keepalive: true,
      });
  void request.catch(() => undefined);
}

function platform(): 'linux' | 'macos' | 'windows' | 'ios' | 'android' | 'other' {
  const text = `${navigator.platform} ${navigator.userAgent}`.toLowerCase();
  if (text.includes('android')) return 'android';
  if (/iphone|ipad|ipod/.test(text)) return 'ios';
  if (text.includes('win')) return 'windows';
  if (text.includes('mac')) return 'macos';
  if (text.includes('linux')) return 'linux';
  return 'other';
}

export function sendAppOpened(): void {
  if (appOpenedSent) return;
  appOpenedSent = true;
  sendProductTelemetry({ type: 'App.opened', properties: { platform: platform() } });
}

function numberBucket(value: number | null | undefined, edges: Array<[number, string]>, final: string): string {
  if (value == null || !Number.isFinite(value) || value < 0) return 'unknown';
  for (const [upper, label] of edges) if (value < upper) return label;
  return final;
}

export function durationBucket(milliseconds: number | null | undefined): DurationBucket {
  return numberBucket(milliseconds, [[1_000, 'lt_1s'], [10_000, '1_10s'], [60_000, '10_60s'], [600_000, '1_10m']], 'gte_10m') as DurationBucket;
}

export function byteBucket(bytes: number | null | undefined): ByteBucket {
  return numberBucket(bytes, [[1, 'zero'], [1024 ** 2, 'lt_1mb'], [100 * 1024 ** 2, '1_100mb'], [1024 ** 3, '100mb_1gb']], 'gte_1gb') as ByteBucket;
}

export function rowBucket(rows: number | null | undefined): RowBucket {
  return numberBucket(rows, [[1, 'zero'], [1_000, '1_999'], [100_000, '1k_99k'], [1_000_000, '100k_999k']], 'gte_1m') as RowBucket;
}

export function columnBucket(columns: number | null | undefined): ColumnBucket {
  return numberBucket(columns, [[1, 'zero'], [6, '1_5'], [21, '6_20'], [101, '21_100']], 'gte_101') as ColumnBucket;
}

export function failureCategory(error: unknown): FailureCategory {
  const status = typeof error === 'object' && error !== null && 'status' in error
    ? Number((error as { status?: unknown }).status)
    : null;
  if (status === 401 || status === 403) return 'permission';
  if (status === 409) return 'conflict';
  if (status === 429) return 'rate_limited';
  if (error instanceof DOMException && error.name === 'AbortError') return 'timeout';
  if (error instanceof TypeError) return 'network';
  return 'unknown';
}

export function telemetryActionKind(kind: string): string {
  // Only product-owned presentation IDs are safe telemetry dimensions.
  if (Object.prototype.hasOwnProperty.call(ACTION_PLACEMENTS, kind)) return kind;
  return kind.startsWith('plugin.') ? 'plugin' : 'other';
}
