import type { HttpContractOperationMap } from "../generated/openHttpContracts";
import { httpContract, type HttpContractSuccessResponse } from "./httpContract";
import { toWireJsonObject } from "./views";
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
  HttpContractSuccessResponse<"tenant.list_alerts.get">;
type AlertRowWire = AlertListWire[number];
type AlertCheckResultWire =
  HttpContractSuccessResponse<"tenant.check_alert.post">;
type AlertChecksPageWire =
  HttpContractSuccessResponse<"tenant.list_alert_checks.get">;
type AlertCheckEventsPageWire =
  HttpContractSuccessResponse<"tenant.list_alert_check_events.get">;

type AlertCheckWire = AlertCheckResultWire["run"];
type AlertMatchWire = AlertCheckResultWire["hits"][number];
type AlertCheckEventWire = AlertCheckEventsPageWire["events"][number];

type AlertCreateBody = ContractRequestBody<"tenant.create_alert.post">;
type AlertPatchBody = ContractRequestBody<"tenant.patch_alert.patch">;

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
  alertId: wire.alert_id,
  status: wire.status,
  changeCursorBefore: wire.change_cursor_before,
  changeCursorAfter: wire.change_cursor_after,
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
  lastEvaluatedChange: wire.last_evaluated_change ?? 0,
  lastRunId: wire.last_run_id,
  lastStatus: wire.last_status,
  createdAt: sqliteTimestampToIso(wire.created_at),
  updatedAt: sqliteTimestampToIso(wire.updated_at),
  latestRun: wire.latest_run ? toAlertCheck(wire.latest_run) : null,
});

const toAlertChecksPage = (wire: AlertChecksPageWire): AlertChecksPage => ({
  schemaVersion: "frisket.alert_checks_page.v1",
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
  alertId: wire.alert_id,
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
  schemaVersion: "frisket.alert_check_events_page.v1",
  order: "asc",
  offset: wire.offset,
  limit: wire.limit,
  total: wire.total,
  hasMore: wire.has_more,
  nextOffset: wire.next_offset,
  events: wire.events.map(toAlertCheckEvent),
});

const toAlertCheckResult = (wire: AlertCheckResultWire): AlertCheckResult => ({
  schemaVersion: "frisket.alert_check.v1",
  alert: toAlertInfo(wire.alert),
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

/** Alert lifecycle and check history for one project. */
export function createAlertsApi(projectId: string) {
  return {
    async listAlerts() {
      return httpContract(
        "tenant.list_alerts.get",
        { pathParams: { pid: projectId } },
        (wire) => wire.map(toAlertInfo),
      );
    },

    async createAlert(input: AlertInput) {
      return httpContract(
        "tenant.create_alert.post",
        { pathParams: { pid: projectId }, body: toWireAlertBody(input) },
        toAlertInfo,
      );
    },

    async updateAlert(alertId: number, input: AlertPatchInput) {
      return httpContract(
        "tenant.patch_alert.patch",
        { pathParams: { pid: projectId, alert_id: alertId }, body: toWireAlertPatch(input) },
        toAlertInfo,
      );
    },

    async deleteAlert(alertId: number): Promise<void> {
      await httpContract(
        "tenant.delete_alert.delete",
        { pathParams: { pid: projectId, alert_id: alertId } },
      );
    },

    /** A bodyless POST: the route declares no request body. */
    async checkAlert(alertId: number) {
      return httpContract(
        "tenant.check_alert.post",
        { pathParams: { pid: projectId, alert_id: alertId } },
        toAlertCheckResult,
      );
    },

    async getAlertChecks(alertId: number, offset = 0, limit = 20) {
      return httpContract(
        "tenant.list_alert_checks.get",
        { pathParams: { pid: projectId, alert_id: alertId }, query: { offset, limit } },
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
        "tenant.list_alert_check_events.get",
        {
          pathParams: { pid: projectId, alert_id: alertId, check_id: runId },
          query: { offset, limit, event_kind: eventKind || undefined },
        },
        toAlertCheckEventsPage,
      );
    },
  };
}
