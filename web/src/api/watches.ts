import type { HttpContractOperationMap } from "../generated/openHttpContracts";
import { httpContract, type HttpContractSuccessResponse } from "./httpContract";
import { toWireJsonObject } from "./viewsLenses";
import type {
  WatchHit,
  WatchInfo,
  WatchInput,
  WatchPatchInput,
  WatchRun,
  WatchRunEvent,
  WatchRunEventsPage,
  WatchRunResult,
  WatchRunsPage,
} from "./types";

export interface WatchesOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

type ContractRequestBody<Id extends keyof HttpContractOperationMap> = Exclude<
  HttpContractOperationMap[Id]["request"],
  undefined
>;

export type WatchListWire =
  HttpContractSuccessResponse<"tenant.list_watches.get">;
export type WatchRowWire = WatchListWire[number];
export type WatchCreatedWire =
  HttpContractSuccessResponse<"tenant.create_watch.post">;
export type WatchPatchedWire =
  HttpContractSuccessResponse<"tenant.patch_watch.patch">;
export type WatchDeletedWire =
  HttpContractSuccessResponse<"tenant.delete_watch.delete">;
export type WatchRunResultWire =
  HttpContractSuccessResponse<"tenant.run_watch.post">;
export type WatchRunsPageWire =
  HttpContractSuccessResponse<"tenant.list_watch_runs.get">;
export type WatchRunEventsPageWire =
  HttpContractSuccessResponse<"tenant.list_watch_run_events.get">;

export type WatchRunWire = WatchRunResultWire["run"];
export type WatchHitWire = WatchRunResultWire["hits"][number];
export type WatchRunEventWire = WatchRunEventsPageWire["events"][number];

export type WatchCreateBody = ContractRequestBody<"tenant.create_watch.post">;
export type WatchPatchBody = ContractRequestBody<"tenant.patch_watch.patch">;

export interface WatchesApi {
  listWatches(options?: WatchesOptions): Promise<WatchListWire>;
  createWatch(
    body: WatchCreateBody,
    options?: WatchesOptions,
  ): Promise<WatchCreatedWire>;
  updateWatch(
    watchId: number,
    body: WatchPatchBody,
    options?: WatchesOptions,
  ): Promise<WatchPatchedWire>;
  deleteWatch(
    watchId: number,
    options?: WatchesOptions,
  ): Promise<WatchDeletedWire>;
  /** A BODYLESS POST: the route declares no request body, so none rides the
   *  wire. (Base's vestigial `{}` run body is the one recorded client-side
   *  wire delta of this cutover.) */
  runWatch(
    watchId: number,
    options?: WatchesOptions,
  ): Promise<WatchRunResultWire>;
  listWatchRuns(
    watchId: number,
    offset?: number | null,
    limit?: number | null,
    hitsLimit?: number | null,
    options?: WatchesOptions,
  ): Promise<WatchRunsPageWire>;
  listWatchRunEvents(
    watchId: number,
    runId: number,
    offset?: number | null,
    limit?: number | null,
    eventKind?: string | null,
    options?: WatchesOptions,
  ): Promise<WatchRunEventsPageWire>;
}

/** The feature-facing port. The generated transport stays private to this
 * module so real.ts only composes the domain API and delegates to it. */
export interface WatchesDomainApi {
  listWatches(): Promise<WatchInfo[]>;
  createWatch(input: WatchInput): Promise<WatchInfo>;
  updateWatch(watchId: number, input: WatchPatchInput): Promise<WatchInfo>;
  deleteWatch(watchId: number): Promise<void>;
  runWatch(watchId: number): Promise<WatchRunResult>;
  getWatchRuns(
    watchId: number,
    offset?: number,
    limit?: number,
  ): Promise<WatchRunsPage>;
  getWatchRunEvents(
    watchId: number,
    runId: number,
    offset?: number,
    limit?: number,
    eventKind?: string,
  ): Promise<WatchRunEventsPage>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

export function createWatchesApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): WatchesApi {
  return {
    listWatches(options = {}) {
      return httpContract(
        "tenant.list_watches.get",
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    createWatch(body, options = {}) {
      return httpContract(
        "tenant.create_watch.post",
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

    updateWatch(watchId, body, options = {}) {
      return httpContract(
        "tenant.patch_watch.patch",
        {
          pathParams: { pid: projectId, watch_id: watchId },
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    deleteWatch(watchId, options = {}) {
      return httpContract(
        "tenant.delete_watch.delete",
        {
          pathParams: { pid: projectId, watch_id: watchId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    runWatch(watchId, options = {}) {
      return httpContract(
        "tenant.run_watch.post",
        {
          pathParams: { pid: projectId, watch_id: watchId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    listWatchRuns(watchId, offset, limit, hitsLimit, options = {}) {
      return httpContract(
        "tenant.list_watch_runs.get",
        {
          pathParams: { pid: projectId, watch_id: watchId },
          query: {
            offset: offset ?? undefined,
            limit: limit ?? undefined,
            hits_limit: hitsLimit ?? undefined,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    listWatchRunEvents(watchId, runId, offset, limit, eventKind, options = {}) {
      return httpContract(
        "tenant.list_watch_run_events.get",
        {
          pathParams: { pid: projectId, watch_id: watchId, run_id: runId },
          query: {
            offset: offset ?? undefined,
            limit: limit ?? undefined,
            event_kind: eventKind ?? undefined,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}

/** sqlite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS", UTC) → ISO. This is
 * deliberately local: real.ts still uses its copy for other domains. */
const toWatchIso = (timestamp: string): string =>
  timestamp.includes("T") ? timestamp : `${timestamp.replace(" ", "T")}Z`;

const toWatchHit = (wire: WatchHitWire): WatchHit => ({
  runId: wire.run_id,
  sheetId: wire.sheet_id,
  rowId: wire.row_id,
  columnId: wire.column_id,
  rank: wire.rank,
  snippet: wire.snippet,
  isNew: Boolean(wire.is_new),
});

/** Accepts every generated run shape: bare result/latest rows and page rows,
 * whose page-only hits_limit/hits_truncated fields are intentionally dropped. */
const toWatchRun = (wire: WatchRunWire & { hits?: WatchHitWire[] }): WatchRun => ({
  id: wire.id,
  watchId: wire.watch_id,
  status: wire.status,
  opCursorBefore: wire.op_cursor_before,
  opCursorAfter: wire.op_cursor_after,
  matchedRows: wire.matched_rows,
  newRows: wire.new_rows,
  error: wire.error,
  errorCode: wire.error_code ?? null,
  resolvedQueryHash: wire.resolved_query_hash ?? null,
  resolvedQuery: wire.resolved_query ?? {},
  startedAt: toWatchIso(wire.started_at),
  finishedAt: wire.finished_at ? toWatchIso(wire.finished_at) : null,
  ...(wire.hits ? { hits: wire.hits.map(toWatchHit) } : {}),
});

const toWatchInfo = (wire: WatchRowWire): WatchInfo => ({
  id: wire.id,
  name: wire.name,
  scope: wire.scope,
  sheetId: wire.sheet_id,
  query: wire.query ?? {},
  queryVersion: wire.query_version ?? null,
  queryHash: wire.query_hash ?? null,
  enabled: Boolean(wire.enabled),
  lastEvaluatedOp: wire.last_evaluated_op ?? 0,
  lastRunId: wire.last_run_id,
  lastStatus: wire.last_status,
  createdAt: toWatchIso(wire.created_at),
  updatedAt: toWatchIso(wire.updated_at),
  latestRun: wire.latest_run ? toWatchRun(wire.latest_run) : null,
});

const toWatchRunsPage = (wire: WatchRunsPageWire): WatchRunsPage => ({
  schemaVersion: "frisket.watch_runs_page.v1",
  order: "desc",
  offset: wire.offset,
  limit: wire.limit,
  total: wire.total,
  hasMore: wire.has_more,
  nextOffset: wire.next_offset,
  runs: wire.runs.map(toWatchRun),
});

const toWatchRunEvent = (wire: WatchRunEventWire): WatchRunEvent => ({
  id: wire.id,
  runId: wire.run_id,
  watchId: wire.watch_id,
  eventKind: wire.event_kind,
  subjectKind: wire.subject_kind,
  subjectRef: wire.subject_ref ?? {},
  beforeJson: wire.before_json,
  afterJson: wire.after_json,
  deltaJson: wire.delta_json,
  severity: wire.severity,
  rank: wire.rank,
  snippet: wire.snippet,
  createdAt: toWatchIso(wire.created_at),
});

const toWatchRunEventsPage = (
  wire: WatchRunEventsPageWire,
): WatchRunEventsPage => ({
  schemaVersion: "frisket.watch_run_events_page.v1",
  order: "asc",
  offset: wire.offset,
  limit: wire.limit,
  total: wire.total,
  hasMore: wire.has_more,
  nextOffset: wire.next_offset,
  events: wire.events.map(toWatchRunEvent),
});

const toWatchRunResult = (wire: WatchRunResultWire): WatchRunResult => ({
  schemaVersion: "frisket.watch_run.v1",
  watch: toWatchInfo(wire.watch),
  run: toWatchRun(wire.run),
  hits: wire.hits.map(toWatchHit),
});

function toWireWatchBody(input: WatchInput): WatchCreateBody {
  const body = {
    name: input.name,
    query: toWireJsonObject(input.query),
  } as WatchCreateBody;
  if (input.scope !== undefined) body.scope = toWireJsonObject(input.scope);
  if (input.enabled !== undefined) body.enabled = input.enabled;
  return body;
}

function toWireWatchPatch(input: WatchPatchInput): WatchPatchBody {
  const body: WatchPatchBody = {};
  if (input.name !== undefined) body.name = input.name;
  if (input.enabled !== undefined) body.enabled = input.enabled;
  return body;
}

export function createWatchesDomainApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): WatchesDomainApi {
  const transport = createWatchesApi(errorFactory, projectId);
  return {
    async listWatches() {
      return (await transport.listWatches()).map(toWatchInfo);
    },

    async createWatch(input) {
      return toWatchInfo(await transport.createWatch(toWireWatchBody(input)));
    },

    async updateWatch(watchId, input) {
      return toWatchInfo(await transport.updateWatch(watchId, toWireWatchPatch(input)));
    },

    async deleteWatch(watchId) {
      await transport.deleteWatch(watchId);
    },

    async runWatch(watchId) {
      return toWatchRunResult(await transport.runWatch(watchId));
    },

    async getWatchRuns(watchId, offset = 0, limit = 20) {
      return toWatchRunsPage(await transport.listWatchRuns(watchId, offset, limit));
    },

    async getWatchRunEvents(watchId, runId, offset = 0, limit = 50, eventKind) {
      return toWatchRunEventsPage(
        await transport.listWatchRunEvents(
          watchId,
          runId,
          offset,
          limit,
          eventKind || null,
        ),
      );
    },
  };
}
