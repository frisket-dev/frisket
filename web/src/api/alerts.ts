import type { HttpContractOperationMap } from "../generated/openHttpContracts";
import { httpContract, type HttpContractSuccessResponse } from "./httpContract";
import { toWireJsonObject } from "./viewsLenses";
import type {
  ViewAlertConditions,
  AlertMatch,
  AlertInfo,
  AlertInput,
  AlertPatchInput,
  AlertCheck,
  AlertCheckEvent,
  AlertCheckEventsPage,
  AlertCheckResult,
  AlertChecksPage,
} from "./types";
import { sqliteTimestampToIso } from './wireValues';

type ContractRequestBody<Id extends keyof HttpContractOperationMap> = Exclude<
  HttpContractOperationMap[Id]["request"],
  undefined
>;

type AlertListWire =
  HttpContractSuccessResponse<"tenant.list_watches.get">;
type AlertRowWire = AlertListWire[number];
type AlertCheckResultWire =
  HttpContractSuccessResponse<"tenant.run_watch.post">;
type AlertChecksPageWire =
  HttpContractSuccessResponse<"tenant.list_watch_runs.get">;
type AlertCheckEventsPageWire =
  HttpContractSuccessResponse<"tenant.list_watch_run_events.get">;

type AlertCheckWire = AlertCheckResultWire["run"];
type AlertMatchWire = AlertCheckResultWire["hits"][number];
type AlertCheckEventWire = AlertCheckEventsPageWire["events"][number];

type AlertCreateBody = ContractRequestBody<"tenant.create_watch.post">;
type AlertPatchBody = ContractRequestBody<"tenant.patch_watch.patch">;

const toAlertMatch = (wire: AlertMatchWire): AlertMatch => ({
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
const toAlertCheck = (wire: AlertCheckWire & { hits?: AlertMatchWire[] }): AlertCheck => ({
  id: wire.id,
  alertId: wire.watch_id,
  status: wire.status,
  opCursorBefore: wire.op_cursor_before,
  opCursorAfter: wire.op_cursor_after,
  matchedRows: wire.matched_rows,
  newRows: wire.new_rows,
  error: wire.error,
  errorCode: wire.error_code ?? null,
  resolvedQueryHash: wire.resolved_query_hash ?? null,
  resolvedQuery: wire.resolved_query ?? {},
  startedAt: sqliteTimestampToIso(wire.started_at),
  finishedAt: wire.finished_at ? sqliteTimestampToIso(wire.finished_at) : null,
  ...(wire.hits ? { hits: wire.hits.map(toAlertMatch) } : {}),
});

const toAlertInfo = (wire: AlertRowWire): AlertInfo => ({
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
  createdAt: sqliteTimestampToIso(wire.created_at),
  updatedAt: sqliteTimestampToIso(wire.updated_at),
  latestRun: wire.latest_run ? toAlertCheck(wire.latest_run) : null,
});

const toAlertChecksPage = (wire: AlertChecksPageWire): AlertChecksPage => ({
  schemaVersion: "frisket.watch_runs_page.v1",
  order: "desc",
  offset: wire.offset,
  limit: wire.limit,
  total: wire.total,
  hasMore: wire.has_more,
  nextOffset: wire.next_offset,
  runs: wire.runs.map(toAlertCheck),
});

const toAlertCheckEvent = (wire: AlertCheckEventWire): AlertCheckEvent => ({
  id: wire.id,
  runId: wire.run_id,
  alertId: wire.watch_id,
  eventKind: wire.event_kind,
  subjectKind: wire.subject_kind,
  subjectRef: wire.subject_ref ?? {},
  beforeJson: wire.before_json,
  afterJson: wire.after_json,
  deltaJson: wire.delta_json,
  severity: wire.severity,
  rank: wire.rank,
  snippet: wire.snippet,
  createdAt: sqliteTimestampToIso(wire.created_at),
});

const toAlertCheckEventsPage = (
  wire: AlertCheckEventsPageWire,
): AlertCheckEventsPage => ({
  schemaVersion: "frisket.watch_run_events_page.v1",
  order: "asc",
  offset: wire.offset,
  limit: wire.limit,
  total: wire.total,
  hasMore: wire.has_more,
  nextOffset: wire.next_offset,
  events: wire.events.map(toAlertCheckEvent),
});

const toAlertCheckResult = (wire: AlertCheckResultWire): AlertCheckResult => ({
  schemaVersion: "frisket.watch_run.v1",
  alert: toAlertInfo(wire.watch),
  run: toAlertCheck(wire.run),
  hits: wire.hits.map(toAlertMatch),
});

function toWireAlertBody(input: AlertInput): AlertCreateBody {
  return { name: input.name, view_id: input.view_id,
    ...(input.detection_policy ? { detection_policy: toWireJsonObject({ ...input.detection_policy }) } : {}),
    ...(input.schedule !== undefined ? { schedule: input.schedule } : {}),
    ...(input.enabled !== undefined ? { enabled: input.enabled } : {}) };
}

function toWireAlertPatch(input: AlertPatchInput): AlertPatchBody {
  const body: AlertPatchBody = {};
  if (input.name !== undefined) body.name = input.name;
  if (input.enabled !== undefined) body.enabled = input.enabled;
  if (input.schedule !== undefined) body.schedule = input.schedule;
  if (input.detection_policy !== undefined) body.detection_policy = toWireJsonObject({ ...input.detection_policy });
  return body;
}

/** Alert lifecycle and run history for one project. */
export function createAlertsApi(projectId: string) {
  return {
    async listAlerts() {
      return httpContract(
        "tenant.list_watches.get",
        { pathParams: { pid: projectId } },
        (wire) => wire.map(toAlertInfo),
      );
    },

    async createAlert(input: AlertInput) {
      return httpContract(
        "tenant.create_watch.post",
        { pathParams: { pid: projectId }, body: toWireAlertBody(input) },
        toAlertInfo,
      );
    },

    async updateAlert(alertId: number, input: AlertPatchInput) {
      return httpContract(
        "tenant.patch_watch.patch",
        { pathParams: { pid: projectId, watch_id: alertId }, body: toWireAlertPatch(input) },
        toAlertInfo,
      );
    },

    async deleteAlert(alertId: number): Promise<void> {
      await httpContract(
        "tenant.delete_watch.delete",
        { pathParams: { pid: projectId, watch_id: alertId } },
      );
    },

    /** A bodyless POST: the route declares no request body. */
    async checkAlert(alertId: number) {
      return httpContract(
        "tenant.run_watch.post",
        { pathParams: { pid: projectId, watch_id: alertId } },
        toAlertCheckResult,
      );
    },

    async getAlertChecks(alertId: number, offset = 0, limit = 20) {
      return httpContract(
        "tenant.list_watch_runs.get",
        { pathParams: { pid: projectId, watch_id: alertId }, query: { offset, limit } },
        toAlertChecksPage,
      );
    },

    async getAlertCheckEvents(
      alertId: number,
      runId: number,
      offset = 0,
      limit = 50,
      eventKind?: string,
    ) {
      return httpContract(
        "tenant.list_watch_run_events.get",
        {
          pathParams: { pid: projectId, watch_id: alertId, run_id: runId },
          query: { offset, limit, event_kind: eventKind || undefined },
        },
        toAlertCheckEventsPage,
      );
    },
  };
}
