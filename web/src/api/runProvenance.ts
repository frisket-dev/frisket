import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type {
  ProvenanceManifest,
  RunTraceRow,
  RunTraceRowEvidence,
} from './types';

export interface RunProvenanceOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export type RunTraceRowEvidenceWire =
  HttpContractSuccessResponse<'tenant.action_run_trace_row.get'>;
export type ProvenanceManifestWire =
  HttpContractSuccessResponse<'tenant.provenance.get'>;

export interface RunProvenanceApi {
  getRunTraceRow(
    projectId: string,
    runId: string,
    rowId: string,
    columnId?: string | null,
    options?: RunProvenanceOptions,
  ): Promise<RunTraceRowEvidence>;
  getProvenanceManifest(
    projectId: string,
    runsOffset?: number,
    runsLimit?: number,
    receiptsOffset?: number,
    receiptsLimit?: number,
    options?: RunProvenanceOptions,
  ): Promise<ProvenanceManifest>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

// FrisketApi keeps numeric route references as strings. The generated route
// signatures reflect FastAPI's integers; this cast preserves the established
// URL bytes without adding a second runtime parser or validator.
const numericPathRef = (value: string): number => value as unknown as number;

const toIso = (timestamp: string): string =>
  timestamp.includes('T') ? timestamp : `${timestamp.replace(' ', 'T')}Z`;

function actionDisplayNameFromActionKind(actionKind: string): string {
  const normalized = actionKind.replace(/[._-]+/g, ' ').trim();
  if (!normalized) return 'AI run';
  return normalized
    .split(/\s+/)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function mapTraceRow(row: NonNullable<RunTraceRowEvidenceWire['trace']>['row']): RunTraceRow | null {
  if (row === null) return null;
  return {
    rowId: row.row_id != null ? String(row.row_id) : null,
    prompt: row.prompt ?? null,
    rawResponse: row.raw_response ?? null,
    data: row.data ?? null,
    error: row.error ?? null,
    tokensIn: row.tokens_in ?? null,
    tokensOut: row.tokens_out ?? null,
    cost: row.cost ?? null,
    cached: Boolean(row.cached),
    latencyMs: row.latency_ms ?? null,
    retries: Array.isArray(row.retries) ? row.retries.length : 0,
  };
}

function rowEvidence(
  wire: RunTraceRowEvidenceWire,
  requestedColumnId: string | null | undefined,
): RunTraceRowEvidence {
  const trace = wire.trace;
  const run = wire.run;
  const actionKind = trace?.action_kind ?? null;
  return {
    runId: String(wire.run_id),
    rowId: String(wire.row_id),
    columnId: wire.column_id != null ? String(wire.column_id) : requestedColumnId ?? null,
    recorded: wire.recorded,
    status: wire.status,
    run: {
      actionKind: run.action_kind ?? null,
      actionName: run.action_name || (run.action_kind ? actionDisplayNameFromActionKind(run.action_kind) : null),
      status: run.status ?? null,
      model: run.model ?? null,
      startedAt: run.started_at ?? null,
      finishedAt: run.finished_at ?? null,
      costActual: run.cost_actual ?? null,
    },
    trace: trace
      ? {
          runId: String(trace.run_id),
          traceId: trace.trace_id ?? null,
          actionKind,
          actionName: trace.action_name || (actionKind ? actionDisplayNameFromActionKind(actionKind) : null),
          model: trace.model ?? null,
          createdAt: trace.created_at ?? null,
          row: mapTraceRow(trace.row),
          recordCount: trace.record_count,
        }
      : null,
  };
}

function manifest(wire: ProvenanceManifestWire): ProvenanceManifest {
  return {
    projectId: wire.project_id,
    models: wire.models.map((model) => ({
      model: model.model,
      provider: model.provider,
      runs: model.runs,
      rows: model.rows,
      cost: model.cost,
    })),
    touched: wire.touched,
    providers: wire.providers,
    totalCost: wire.total_cost,
    hasUnknownCosts: wire.has_unknown_costs,
    unknownCostRuns: wire.unknown_cost_runs,
    actionKinds: wire.action_kinds.map((actionKind) => ({
      actionKind: actionKind.action_kind,
      actionName: actionKind.action_name,
      runs: actionKind.runs,
      rows: actionKind.rows,
      failedRows: actionKind.failed_rows,
      cost: actionKind.cost,
    })),
    runs: wire.runs.map((run) => ({
      runId: String(run.run_id),
      sheetId: String(run.sheet_id),
      actionKind: run.action_kind,
      actionName: run.action_name,
      model: run.model,
      provider: run.provider,
      status: run.status,
      totalRows: run.total_rows,
      completedRows: run.completed_rows,
      failedRows: run.failed_rows,
      cost: run.cost,
      startedAt: run.started_at ? toIso(run.started_at) : null,
      finishedAt: run.finished_at ? toIso(run.finished_at) : null,
    })),
    runsPage: {
      schemaVersion: wire.runs_page.schema_version,
      order: wire.runs_page.order,
      offset: wire.runs_page.offset,
      limit: wire.runs_page.limit,
      total: wire.runs_page.total,
      hasMore: wire.runs_page.has_more,
      nextOffset: wire.runs_page.next_offset,
    },
    receipts: wire.receipts.map((receipt) => ({
      receiptId: receipt.receipt_id,
      actionKind: receipt.action_kind,
      status: receipt.status,
      runId: receipt.run_id == null ? null : String(receipt.run_id),
      createdAt: receipt.created_at ? toIso(receipt.created_at) : null,
    })),
    receiptsPage: {
      schemaVersion: wire.receipts_page.schema_version,
      order: wire.receipts_page.order,
      offset: wire.receipts_page.offset,
      limit: wire.receipts_page.limit,
      total: wire.receipts_page.total,
      hasMore: wire.receipts_page.has_more,
      nextOffset: wire.receipts_page.next_offset,
    },
  };
}

export function createRunProvenanceApi(errorFactory: ContractErrorFactory): RunProvenanceApi {
  return {
    getRunTraceRow(projectId, runId, rowId, columnId, options = {}) {
      return httpContract(
        'tenant.action_run_trace_row.get',
        {
          pathParams: {
            pid: projectId,
            run_id: numericPathRef(runId),
            row_id: numericPathRef(rowId),
          },
          query: { column_id: columnId == null ? undefined : numericPathRef(columnId) },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        (wire) => rowEvidence(wire, columnId),
      );
    },

    getProvenanceManifest(
      projectId,
      runsOffset = 0,
      runsLimit = 25,
      receiptsOffset = 0,
      receiptsLimit = 25,
      options = {},
    ) {
      return httpContract(
        'tenant.provenance.get',
        {
          pathParams: { pid: projectId },
          query: {
            runs_offset: runsOffset,
            runs_limit: runsLimit,
            receipts_offset: receiptsOffset,
            receipts_limit: receiptsLimit,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        manifest,
      );
    },
  };
}
