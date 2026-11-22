import { ApiError, firstNonEmptyString } from './contractErrors';
import type { GeneratedActionParams } from '../generated/actionTypes';
import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from './httpContract';
import type {
  JsonValue,
  FeedConfig,
  FeedDetail,
  FeedFetchResult,
  FeedHealth,
  FeedInfo,
  FeedInput,
  FeedCheck,
  FeedCheckPage,
} from './types';
import type { V1ActionSession } from './v1ActionSession';
import { isRecord, sqliteTimestampToIso } from './wireValues';

type ProjectFeedListWire =
  HttpContractSuccessResponse<'tenant.list_feeds.get'>;
type ProjectFeedDetailWire =
  HttpContractSuccessResponse<'tenant.get_feed.get'>;
type ProjectFeedHealthWire =
  HttpContractSuccessResponse<'tenant.get_feed_health.get'>;

interface ProjectFeedsApiDependencies {
  v1ActionSession: V1ActionSession;
}

type ProjectFeedWire = ProjectFeedListWire[number];
type ProjectFeedCheckWire = ProjectFeedDetailWire['runs'][number];
type ProjectFeedCheckPageWire = ProjectFeedDetailWire['runs_page'];
type FeedCreateParams = GeneratedActionParams['feed.create'];
type FeedUpdateParams = GeneratedActionParams['feed.update'];
type FeedUpdatePatch = FeedUpdateParams['patch'];
type FeedDeleteParams = GeneratedActionParams['feed.delete'];

const toFeedInfo = (w: ProjectFeedWire): FeedInfo => ({
  id: w.id,
  name: w.name,
  kind: w.kind,
  url: w.url,
  config: w.config ?? {},
  sheetId: w.sheet_id,
  schedule: w.schedule,
  enabled: Boolean(w.enabled),
  lastCheckedAt: w.last_checked_at ? sqliteTimestampToIso(w.last_checked_at) : null,
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

function feedConfigFromV1Ref(value: unknown): FeedConfig {
  return jsonValueFromUnknown(value) ?? {};
}

function wireFeedFromV1Ref(ref: Record<string, unknown>): FeedInfo {
  const id = Number(ref.feed_id);
  const name = typeof ref.name === 'string' ? ref.name : '';
  const kind = typeof ref.feed_kind === 'string' ? ref.feed_kind : '';
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
    throw new ApiError(500, 'feed.create returned an incomplete feed output');
  }
  return {
    id,
    name,
    kind,
    url: typeof ref.url === 'string' ? ref.url : null,
    config: feedConfigFromV1Ref(ref.config),
    sheetId,
    schedule: typeof ref.schedule === 'string' ? ref.schedule : null,
    enabled: typeof ref.enabled === 'boolean' ? ref.enabled : Boolean(ref.enabled),
    lastCheckedAt: typeof ref.last_checked_at === 'string' ? sqliteTimestampToIso(ref.last_checked_at) : null,
    lastStatus: typeof ref.last_status === 'string' ? ref.last_status : null,
    newRowsTotal,
  };
}

const toFeedCheck = (w: ProjectFeedCheckWire): FeedCheck => ({
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
  startedAt: sqliteTimestampToIso(w.started_at),
  finishedAt: w.finished_at ? sqliteTimestampToIso(w.finished_at) : null,
});

const toFeedCheckPage = (w: ProjectFeedCheckPageWire): FeedCheckPage => ({
  schemaVersion: 'frisket.feed_checks_page.v1',
  order: 'desc',
  offset: w.offset ?? 0,
  limit: w.limit ?? 0,
  total: w.total ?? 0,
  hasMore: Boolean(w.has_more),
  nextOffset: w.next_offset ?? null,
  latestRun: w.latest_run ? toFeedCheck(w.latest_run) : null,
  latestRunLoaded: Boolean(w.latest_run_loaded),
});

const toFeedHealth = (w: ProjectFeedHealthWire): FeedHealth => ({
  schemaVersion: w.schema_version,
  feed: {
    id: w.feed.id,
    name: w.feed.name,
    kind: w.feed.kind,
    url: w.feed.url,
    enabled: Boolean(w.feed.enabled),
    schedule: w.feed.schedule,
    sheetId: w.feed.sheet_id,
    createdAt: w.feed.created_at ? sqliteTimestampToIso(w.feed.created_at) : null,
    redactions: w.feed.redactions ?? [],
  },
  summary: {
    status: w.summary.status,
    lastSuccessAt: w.summary.last_success_at ? sqliteTimestampToIso(w.summary.last_success_at) : null,
    lastFailureAt: w.summary.last_failure_at ? sqliteTimestampToIso(w.summary.last_failure_at) : null,
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
    schemaVersion: 'frisket.feed_checks_page.v1',
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
    startedAt: sqliteTimestampToIso(run.started_at),
    finishedAt: run.finished_at ? sqliteTimestampToIso(run.finished_at) : null,
    feedId: run.feed_id,
    changeId: run.change_id ?? null,
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
    createdAt: job.created_at ? sqliteTimestampToIso(job.created_at) : null,
    startedAt: job.started_at ? sqliteTimestampToIso(job.started_at) : null,
    finishedAt: job.finished_at ? sqliteTimestampToIso(job.finished_at) : null,
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
    message: firstNonEmptyString(notice.message, String(notice.code ?? 'Feed alert')),
  })),
  warnings: w.warnings.map((notice) => ({
    ...notice,
    message: firstNonEmptyString(notice.message, String(notice.code ?? 'Feed warning')),
  })),
});

function feedConfigParam(
  value: Record<string, unknown>,
): NonNullable<FeedCreateParams['config']> {
  const config = jsonValueFromUnknown(value);
  if (!isRecord(config)) {
    throw new ApiError(400, 'Feed config must contain only JSON values');
  }
  return config;
}

function toFeedCreateParams(input: FeedInput): FeedCreateParams {
  return {
    name: input.name,
    kind: input.kind,
    ...(input.url !== undefined ? { url: input.url } : {}),
    ...(input.schedule !== undefined ? { schedule: input.schedule } : {}),
    ...(input.enabled !== undefined ? { enabled: input.enabled } : {}),
    ...(input.config !== undefined ? { config: feedConfigParam(input.config) } : {}),
  };
}

/** Drop undefined keys so a PATCH only carries the fields the form changed
 *  (the backend treats null as "clear", undefined as "leave alone"). */
function toFeedUpdatePatch(input: Partial<FeedInput>): FeedUpdatePatch {
  const out: FeedUpdatePatch = {};
  if (input.name !== undefined) out.name = input.name;
  if (input.kind !== undefined) out.kind = input.kind;
  if (input.url !== undefined) out.url = input.url;
  if (input.schedule !== undefined) out.schedule = input.schedule;
  if (input.enabled !== undefined) out.enabled = input.enabled;
  if (input.config !== undefined) out.config = feedConfigParam(input.config);
  return out;
}

export function createProjectFeedsApi({   v1ActionSession, }: ProjectFeedsApiDependencies, projectId: string) {
  return {
    /** All configured feeds for the project (empty when none). */
    async listFeeds(options: RequestOptions = {}): Promise<FeedInfo[]> {
      const wire = await httpContract(
        'tenant.list_feeds.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
      return wire.map(toFeedInfo);
    },

    /** One feed with its recent check history. */
    async getFeed(
      feedId: number,
      runsOffset?: number | null,
      runsLimit = 50,
      options: RequestOptions = {},
    ): Promise<FeedDetail> {
      const wire = await httpContract(
        'tenant.get_feed.get',
        {
          pathParams: { pid: projectId, feed_id: feedId },
          query: {
            runs_offset: runsOffset ?? undefined,
            runs_limit: runsLimit,
          },
          signal: options.signal,
        },
      );
      return {
        ...toFeedInfo(wire),
        runs: wire.runs.map(toFeedCheck),
        runsPage: toFeedCheckPage(wire.runs_page),
      };
    },

    /** Redacted operational health for one feed. */
    async getFeedHealth(
      feedId: number,
      runsOffset?: number | null,
      runsLimit = 20,
      options: RequestOptions = {},
    ): Promise<FeedHealth> {
      const wire = await httpContract(
        'tenant.get_feed_health.get',
        {
          pathParams: { pid: projectId, feed_id: feedId },
          query: {
            runs_offset: runsOffset ?? undefined,
            runs_limit: runsLimit,
          },
          signal: options.signal,
        },
      );
      return toFeedHealth(wire);
    },

    async createFeed(input: FeedInput): Promise<FeedInfo> {
      const params = toFeedCreateParams(input);
      const spec = v1ActionSession.registeredProjectActionSpec('feed.create', params);
      return v1ActionSession.withV1ActionResult(spec, (out) => {
        const feedRef = (out.outputs ?? []).find((candidate) => (
          candidate.kind === 'feed' && isRecord(candidate.ref)
        ))?.ref;
        if (!isRecord(feedRef)) {
          throw new ApiError(500, 'feed.create did not return feed output');
        }
        return wireFeedFromV1Ref(feedRef);
      }, { clearIdempotency: 'finally' });
    },

    async updateFeed(feedId: number, patch: Partial<FeedInput>): Promise<FeedInfo> {
      const params: FeedUpdateParams = {
        feed_id: feedId,
        patch: toFeedUpdatePatch(patch),
      };
      const spec = v1ActionSession.registeredProjectActionSpec('feed.update', params);
      return v1ActionSession.withV1ActionResult(spec, (out) => {
        const feedRef = (out.outputs ?? []).find((candidate) => (
          candidate.kind === 'feed' && isRecord(candidate.ref)
        ))?.ref;
        if (!isRecord(feedRef)) {
          throw new ApiError(500, 'feed.update did not return feed output');
        }
        return wireFeedFromV1Ref(feedRef);
      }, { clearIdempotency: 'finally' });
    },

    async deleteFeed(feedId: number): Promise<void> {
      const params: FeedDeleteParams = { feed_id: feedId };
      const spec = v1ActionSession.registeredProjectActionSpec('feed.delete', params);
      await v1ActionSession.withV1ActionResult(
        spec,
        () => undefined,
        { clearIdempotency: 'finally' },
      );
    },

    /** Manual "fetch now" through generic feed.poll. Rejects on ingest failure;
     *  the failed poll is still recorded in the feed's check history. */
    async fetchFeed(feedId: number): Promise<FeedFetchResult> {
      if (!Number.isInteger(feedId) || feedId <= 0) {
        throw new ApiError(400, `Feed ${feedId} is not a valid v1 feed ref`);
      }
      const spec = v1ActionSession.registeredProjectActionSpec(
        'feed.poll', { feed: feedId },
      );
      return v1ActionSession.withV1ActionResult(spec, (out) => {
        if (out.status === 'queued' && out.job_id != null) {
          const sheet = out.outputs?.find((output) => output.kind === 'sheet')?.sheet_id;
          if (sheet == null) throw new ApiError(500, 'Queued feed poll did not return its sheet');
          return { jobId: out.job_id, sheetId: sheet, runId: null,
            newRows: 0, revisions: 0, skippedRows: 0, changedRows: 0, warningCount: 0 };
        }
        const feedCheck = (out.outputs ?? []).find((candidate) => (
          candidate.kind === 'feed_check' && isRecord(candidate.ref)
        ))?.ref;
        if (!isRecord(feedCheck)) {
          throw new ApiError(500, 'feed.poll did not return feed check output');
        }
        const runId = Number(feedCheck.feed_check_id);
        const newRows = Number(feedCheck.new_rows ?? 0);
        const revisions = Number(feedCheck.revisions ?? 0);
        const skippedRows = Number(feedCheck.skipped_rows ?? 0);
        const changedRows = Number(feedCheck.changed_rows ?? 0);
        const warningCount = Number(feedCheck.warning_count ?? 0);
        const sheetId = feedCheck.sheet_id == null ? null : Number(feedCheck.sheet_id);
        if (
          !Number.isInteger(runId) ||
          !Number.isInteger(newRows) ||
          !Number.isInteger(revisions) ||
          !Number.isInteger(skippedRows) ||
          !Number.isInteger(changedRows) ||
          !Number.isInteger(warningCount) ||
          (sheetId !== null && !Number.isInteger(sheetId))
        ) {
          throw new ApiError(500, 'feed.poll feed check output was incomplete');
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
