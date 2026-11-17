import type { HttpContractOperationMap } from "../generated/openHttpContracts";
import { httpContract, type HttpContractSuccessResponse } from "./httpContract";
import { toWireJsonObject } from "./viewsLenses";
import type {
  ViewAlertConditions,
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

type ContractRequestBody<Id extends keyof HttpContractOperationMap> = Exclude<
  HttpContractOperationMap[Id]["request"],
  undefined
>;

type WatchListWire =
  HttpContractSuccessResponse<"tenant.list_watches.get">;
type WatchRowWire = WatchListWire[number];
type WatchRunResultWire =
  HttpContractSuccessResponse<"tenant.run_watch.post">;
type WatchRunsPageWire =
  HttpContractSuccessResponse<"tenant.list_watch_runs.get">;
type WatchRunEventsPageWire =
  HttpContractSuccessResponse<"tenant.list_watch_run_events.get">;

type WatchRunWire = WatchRunResultWire["run"];
type WatchHitWire = WatchRunResultWire["hits"][number];
type WatchRunEventWire = WatchRunEventsPageWire["events"][number];

type WatchCreateBody = ContractRequestBody<"tenant.create_watch.post">;
type WatchPatchBody = ContractRequestBody<"tenant.patch_watch.patch">;

/** sqlite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS", UTC) → ISO. */
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
  viewId: wire.view_id,
  sheetId: wire.sheet_id,
  schedule: wire.schedule,
  detectionPolicy: wire.detection_policy as unknown as ViewAlertConditions,
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
  return { name: input.name, view_id: input.view_id,
    ...(input.detection_policy ? { detection_policy: toWireJsonObject({ ...input.detection_policy }) } : {}),
    ...(input.schedule !== undefined ? { schedule: input.schedule } : {}),
    ...(input.enabled !== undefined ? { enabled: input.enabled } : {}) };
}

function toWireWatchPatch(input: WatchPatchInput): WatchPatchBody {
  const body: WatchPatchBody = {};
  if (input.name !== undefined) body.name = input.name;
  if (input.enabled !== undefined) body.enabled = input.enabled;
  if (input.schedule !== undefined) body.schedule = input.schedule;
  if (input.detection_policy !== undefined) body.detection_policy = toWireJsonObject({ ...input.detection_policy });
  return body;
}

/** Watch lifecycle and run history for one project. */
export function createWatchesApi(projectId: string) {
  return {
    async listWatches() {
      return httpContract(
        "tenant.list_watches.get",
        { pathParams: { pid: projectId } },
        (wire) => wire.map(toWatchInfo),
      );
    },

    async createWatch(input: WatchInput) {
      return httpContract(
        "tenant.create_watch.post",
        { pathParams: { pid: projectId }, body: toWireWatchBody(input) },
        toWatchInfo,
      );
    },

    async updateWatch(watchId: number, input: WatchPatchInput) {
      return httpContract(
        "tenant.patch_watch.patch",
        { pathParams: { pid: projectId, watch_id: watchId }, body: toWireWatchPatch(input) },
        toWatchInfo,
      );
    },

    async deleteWatch(watchId: number): Promise<void> {
      await httpContract(
        "tenant.delete_watch.delete",
        { pathParams: { pid: projectId, watch_id: watchId } },
      );
    },

    /** A bodyless POST: the route declares no request body. */
    async runWatch(watchId: number) {
      return httpContract(
        "tenant.run_watch.post",
        { pathParams: { pid: projectId, watch_id: watchId } },
        toWatchRunResult,
      );
    },

    async getWatchRuns(watchId: number, offset = 0, limit = 20) {
      return httpContract(
        "tenant.list_watch_runs.get",
        { pathParams: { pid: projectId, watch_id: watchId }, query: { offset, limit } },
        toWatchRunsPage,
      );
    },

    async getWatchRunEvents(
      watchId: number,
      runId: number,
      offset = 0,
      limit = 50,
      eventKind?: string,
    ) {
      return httpContract(
        "tenant.list_watch_run_events.get",
        {
          pathParams: { pid: projectId, watch_id: watchId, run_id: runId },
          query: { offset, limit, event_kind: eventKind || undefined },
        },
        toWatchRunEventsPage,
      );
    },
  };
}
