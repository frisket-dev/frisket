import { ApiError, firstNonEmptyString } from './contractErrors';
import type { GeneratedActionParams } from '../generated/actionTypes';
import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type {
  JsonValue,
  SourceConfig,
  SourceDetail,
  SourceFetchResult,
  SourceHealth,
  SourceInfo,
  SourceInput,
  SourceRun,
  SourceRunPage,
} from './types';
import type { V1ActionSession } from './v1ActionSession';

export interface ProjectSourcesOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export type ProjectSourceListWire =
  HttpContractSuccessResponse<'tenant.list_sources.get'>;
export type ProjectSourceDetailWire =
  HttpContractSuccessResponse<'tenant.get_source_ep.get'>;
export type ProjectSourceHealthWire =
  HttpContractSuccessResponse<'tenant.get_source_health_ep.get'>;

export interface ProjectSourcesApi {
  listSources(options?: ProjectSourcesOptions): Promise<SourceInfo[]>;
  getSource(
    sourceId: number,
    runsOffset?: number | null,
    runsLimit?: number,
    options?: ProjectSourcesOptions,
  ): Promise<SourceDetail>;
  getSourceHealth(
    sourceId: number,
    runsOffset?: number | null,
    runsLimit?: number,
    options?: ProjectSourcesOptions,
  ): Promise<SourceHealth>;
  createSource(input: SourceInput): Promise<SourceInfo>;
  updateSource(sourceId: number, patch: Partial<SourceInput>): Promise<SourceInfo>;
  deleteSource(sourceId: number): Promise<void>;
  fetchSource(sourceId: number): Promise<SourceFetchResult>;
}

export interface ProjectSourcesApiDependencies {
  errorFactory: ContractErrorFactory;
  v1ActionSession: V1ActionSession;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type ProjectSourceWire = ProjectSourceListWire[number];
type ProjectSourceRunWire = ProjectSourceDetailWire['runs'][number];
type ProjectSourceRunPageWire = ProjectSourceDetailWire['runs_page'];
type SourceCreateParams = GeneratedActionParams['source.create'];
type SourceUpdateParams = GeneratedActionParams['source.update'];
type SourceUpdatePatch = SourceUpdateParams['patch'];
type SourceDeleteParams = GeneratedActionParams['source.delete'];

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** sqlite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS", UTC) → ISO. */
const toIso = (ts: string): string =>
  ts.includes('T') ? ts : `${ts.replace(' ', 'T')}Z`;

const toSourceInfo = (w: ProjectSourceWire): SourceInfo => ({
  id: w.id,
  name: w.name,
  kind: w.kind,
  url: w.url,
  config: w.config ?? {},
  sheetId: w.sheet_id,
  schedule: w.schedule,
  enabled: Boolean(w.enabled),
  lastCheckedAt: w.last_checked_at ? toIso(w.last_checked_at) : null,
  lastStatus: w.last_status,
  newRowsTotal: w.new_rows_total ?? 0,
});

function jsonValueFromUnknown(value: unknown): JsonValue | undefined {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'number') return Number.isFinite(value) ? value : undefined;
  if (Array.isArray(value)) {
    const items: JsonValue[] = [];
    for (const item of value) {
      const json = jsonValueFromUnknown(item);
      if (json === undefined) return undefined;
      items.push(json);
    }
    return items;
  }
  if (isRecord(value)) {
    const record: { [key: string]: JsonValue } = {};
    for (const [key, item] of Object.entries(value)) {
      const json = jsonValueFromUnknown(item);
      if (json === undefined) return undefined;
      record[key] = json;
    }
    return record;
  }
  return undefined;
}

function sourceConfigFromV1Ref(value: unknown): SourceConfig {
  return jsonValueFromUnknown(value) ?? {};
}

function wireSourceFromV1Ref(ref: Record<string, unknown>): SourceInfo {
  const id = Number(ref.source_id);
  const name = typeof ref.name === 'string' ? ref.name : '';
  const kind = typeof ref.source_kind === 'string' ? ref.source_kind : '';
  const sheetId = ref.sheet_id == null ? null : Number(ref.sheet_id);
  const newRowsTotal = Number(ref.new_rows_total ?? 0);
  if (
    !Number.isInteger(id) ||
    id <= 0 ||
    !name ||
    !kind ||
    (sheetId !== null && !Number.isInteger(sheetId)) ||
    !Number.isInteger(newRowsTotal)
  ) {
    throw new ApiError(500, 'source.create returned an incomplete source output');
  }
  return {
    id,
    name,
    kind,
    url: typeof ref.url === 'string' ? ref.url : null,
    config: sourceConfigFromV1Ref(ref.config),
    sheetId,
    schedule: typeof ref.schedule === 'string' ? ref.schedule : null,
    enabled: typeof ref.enabled === 'boolean' ? ref.enabled : Boolean(ref.enabled),
    lastCheckedAt: typeof ref.last_checked_at === 'string' ? toIso(ref.last_checked_at) : null,
    lastStatus: typeof ref.last_status === 'string' ? ref.last_status : null,
    newRowsTotal,
  };
}

const toSourceRun = (w: ProjectSourceRunWire): SourceRun => ({
  id: w.id,
  status: w.status,
  newRows: w.new_rows ?? 0,
  skippedRows: w.skipped_rows ?? 0,
  changedRows: w.changed_rows ?? 0,
  revisions: w.revisions ?? 0,
  warningCount: w.warning_count ?? 0,
  durationMs: w.duration_ms ?? null,
  costMicro: w.cost_micro ?? 0,
  receiptId: w.receipt_id ?? null,
  error: w.error ?? null,
  startedAt: toIso(w.started_at),
  finishedAt: w.finished_at ? toIso(w.finished_at) : null,
});

const toSourceRunPage = (w: ProjectSourceRunPageWire): SourceRunPage => ({
  schemaVersion: 'frisket.source_runs_page.v1',
  order: 'desc',
  offset: w.offset ?? 0,
  limit: w.limit ?? 0,
  total: w.total ?? 0,
  hasMore: Boolean(w.has_more),
  nextOffset: w.next_offset ?? null,
  latestRun: w.latest_run ? toSourceRun(w.latest_run) : null,
  latestRunLoaded: Boolean(w.latest_run_loaded),
});

const toSourceHealth = (w: ProjectSourceHealthWire): SourceHealth => ({
  schemaVersion: w.schema_version,
  source: {
    id: w.source.id,
    name: w.source.name,
    kind: w.source.kind,
    url: w.source.url,
    enabled: Boolean(w.source.enabled),
    schedule: w.source.schedule,
    sheetId: w.source.sheet_id,
    createdAt: w.source.created_at ? toIso(w.source.created_at) : null,
    redactions: w.source.redactions ?? [],
  },
  summary: {
    status: w.summary.status,
    lastSuccessAt: w.summary.last_success_at ? toIso(w.summary.last_success_at) : null,
    lastFailureAt: w.summary.last_failure_at ? toIso(w.summary.last_failure_at) : null,
    consecutiveFailures: w.summary.consecutive_failures ?? 0,
    newRowsTotal: w.summary.new_rows_total ?? 0,
    newRowsRecent: w.summary.new_rows_recent ?? 0,
    changedRowsRecent: w.summary.changed_rows_recent ?? 0,
    skippedRowsRecent: w.summary.skipped_rows_recent ?? 0,
    revisionsRecent: w.summary.revisions_recent ?? 0,
    recentRunCount: w.summary.recent_run_count ?? 0,
    lastCursorSummary: w.summary.last_cursor_summary ?? 'not_recorded',
  },
  runsPage: {
    schemaVersion: 'frisket.source_runs_page.v1',
    order: 'desc',
    offset: w.runs_page.offset ?? 0,
    limit: w.runs_page.limit ?? 0,
    total: w.runs_page.total ?? 0,
    hasMore: Boolean(w.runs_page.has_more),
    nextOffset: w.runs_page.next_offset ?? null,
  },
  runs: w.runs.map((run) => ({
    id: run.id,
    status: run.status,
    newRows: run.new_rows ?? 0,
    skippedRows: run.skipped_rows ?? 0,
    changedRows: run.changed_rows ?? 0,
    revisions: run.revisions ?? 0,
    warningCount: run.warning_count ?? 0,
    durationMs: run.duration_ms ?? null,
    costMicro: run.cost_micro ?? 0,
    receiptId: run.receipt_id ?? null,
    error: run.error_summary ?? null,
    startedAt: toIso(run.started_at),
    finishedAt: run.finished_at ? toIso(run.finished_at) : null,
    sourceId: run.source_id,
    opId: run.op_id ?? null,
    errorSummary: run.error_summary ?? null,
    cursorBeforePresent: Boolean(run.cursor_before_present),
    cursorAfterPresent: Boolean(run.cursor_after_present),
    summaryPresent: Boolean(run.summary_present),
  })),
  downstreamJobs: w.downstream_jobs.map((job) => ({
    jobId: job.job_id,
    kind: job.kind,
    status: job.status,
    attempts: job.attempts ?? 0,
    maxAttempts: job.max_attempts ?? 0,
    createdAt: job.created_at ? toIso(job.created_at) : null,
    startedAt: job.started_at ? toIso(job.started_at) : null,
    finishedAt: job.finished_at ? toIso(job.finished_at) : null,
    refs: job.refs ?? {},
    resultSummary: job.result_summary ?? {},
    errorSummary: job.error_summary ?? null,
    stalled: Boolean(job.stalled),
  })),
  costs: {
    recentActualMicro: w.costs?.recent_actual_micro ?? 0,
    recentEstimatedMicro: w.costs?.recent_estimated_micro ?? 0,
    basis: w.costs?.basis ?? 'unknown',
  },
  alerts: w.alerts.map((notice) => ({
    ...notice,
    message: firstNonEmptyString(notice.message, String(notice.code ?? 'Source alert')),
  })),
  warnings: w.warnings.map((notice) => ({
    ...notice,
    message: firstNonEmptyString(notice.message, String(notice.code ?? 'Source warning')),
  })),
});

function sourceConfigParam(
  value: Record<string, unknown>,
): NonNullable<SourceCreateParams['config']> {
  const config = jsonValueFromUnknown(value);
  if (!isRecord(config)) {
    throw new ApiError(400, 'Source config must contain only JSON values');
  }
  return config;
}

function toSourceCreateParams(input: SourceInput): SourceCreateParams {
  return {
    name: input.name,
    kind: input.kind,
    ...(input.url !== undefined ? { url: input.url } : {}),
    ...(input.schedule !== undefined ? { schedule: input.schedule } : {}),
    ...(input.enabled !== undefined ? { enabled: input.enabled } : {}),
    ...(input.config !== undefined ? { config: sourceConfigParam(input.config) } : {}),
  };
}

/** Drop undefined keys so a PATCH only carries the fields the form changed
 *  (the backend treats null as "clear", undefined as "leave alone"). */
function toSourceUpdatePatch(input: Partial<SourceInput>): SourceUpdatePatch {
  const out: SourceUpdatePatch = {};
  if (input.name !== undefined) out.name = input.name;
  if (input.kind !== undefined) out.kind = input.kind;
  if (input.url !== undefined) out.url = input.url;
  if (input.schedule !== undefined) out.schedule = input.schedule;
  if (input.enabled !== undefined) out.enabled = input.enabled;
  if (input.config !== undefined) out.config = sourceConfigParam(input.config);
  return out;
}

export function createProjectSourcesApi({
  errorFactory,
  v1ActionSession,
}: ProjectSourcesApiDependencies, projectId: string): ProjectSourcesApi {
  return {
    async listSources(options = {}) {
      const wire = await httpContract(
        'tenant.list_sources.get',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
      return wire.map(toSourceInfo);
    },

    async getSource(sourceId, runsOffset, runsLimit = 50, options = {}) {
      const wire = await httpContract(
        'tenant.get_source_ep.get',
        {
          pathParams: { pid: projectId, source_id: sourceId },
          query: {
            runs_offset: runsOffset ?? undefined,
            runs_limit: runsLimit,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
      return {
        ...toSourceInfo(wire),
        runs: wire.runs.map(toSourceRun),
        runsPage: toSourceRunPage(wire.runs_page),
      };
    },

    async getSourceHealth(sourceId, runsOffset, runsLimit = 20, options = {}) {
      const wire = await httpContract(
        'tenant.get_source_health_ep.get',
        {
          pathParams: { pid: projectId, source_id: sourceId },
          query: {
            runs_offset: runsOffset ?? undefined,
            runs_limit: runsLimit,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
      return toSourceHealth(wire);
    },

    async createSource(input) {
      const params = toSourceCreateParams(input);
      const spec = v1ActionSession.registeredProjectActionSpec('source.create', params);
      return v1ActionSession.withV1ActionResult(spec, (out) => {
        const sourceRef = (out.outputs ?? []).find((candidate) => (
          candidate.kind === 'source' && isRecord(candidate.ref)
        ))?.ref;
        if (!isRecord(sourceRef)) {
          throw new ApiError(500, 'source.create did not return source output');
        }
        return wireSourceFromV1Ref(sourceRef);
      }, { clearIdempotency: 'finally' });
    },

    async updateSource(sourceId, patch) {
      const params: SourceUpdateParams = {
        source_id: sourceId,
        patch: toSourceUpdatePatch(patch),
      };
      const spec = v1ActionSession.registeredProjectActionSpec('source.update', params);
      return v1ActionSession.withV1ActionResult(spec, (out) => {
        const sourceRef = (out.outputs ?? []).find((candidate) => (
          candidate.kind === 'source' && isRecord(candidate.ref)
        ))?.ref;
        if (!isRecord(sourceRef)) {
          throw new ApiError(500, 'source.update did not return source output');
        }
        return wireSourceFromV1Ref(sourceRef);
      }, { clearIdempotency: 'finally' });
    },

    async deleteSource(sourceId) {
      const params: SourceDeleteParams = { source_id: sourceId };
      const spec = v1ActionSession.registeredProjectActionSpec('source.delete', params);
      await v1ActionSession.withV1ActionResult(
        spec,
        () => undefined,
        { clearIdempotency: 'finally' },
      );
    },

    async fetchSource(sourceId) {
      if (!Number.isInteger(sourceId) || sourceId <= 0) {
        throw new ApiError(400, `Source ${sourceId} is not a valid v1 source ref`);
      }
      const spec = v1ActionSession.registeredProjectActionSpec(
        'source.poll', { source: sourceId },
      );
      return v1ActionSession.withV1ActionResult(spec, (out) => {
        const sourceRun = (out.outputs ?? []).find((candidate) => (
          candidate.kind === 'source_run' && isRecord(candidate.ref)
        ))?.ref;
        if (!isRecord(sourceRun)) {
          throw new ApiError(500, 'source.poll did not return source run output');
        }
        const runId = Number(sourceRun.source_run_id);
        const newRows = Number(sourceRun.new_rows ?? 0);
        const revisions = Number(sourceRun.revisions ?? 0);
        const skippedRows = Number(sourceRun.skipped_rows ?? 0);
        const changedRows = Number(sourceRun.changed_rows ?? 0);
        const warningCount = Number(sourceRun.warning_count ?? 0);
        const sheetId = sourceRun.sheet_id == null ? null : Number(sourceRun.sheet_id);
        if (
          !Number.isInteger(runId) ||
          !Number.isInteger(newRows) ||
          !Number.isInteger(revisions) ||
          !Number.isInteger(skippedRows) ||
          !Number.isInteger(changedRows) ||
          !Number.isInteger(warningCount) ||
          (sheetId !== null && !Number.isInteger(sheetId))
        ) {
          throw new ApiError(500, 'source.poll source run output was incomplete');
        }
        return {
          runId,
          newRows,
          revisions,
          skippedRows,
          changedRows,
          warningCount,
          sheetId,
        };
      }, { clearIdempotency: 'finally' });
    },
  };
}
