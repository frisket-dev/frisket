import { toRunProgressStatus } from '../../runStatusModel';
import {
  httpContract,
  type HttpContractSuccessResponse,
} from '../httpContract';
import type {
  ActionJob,
  ActionJobsPage,
  RunProgress,
  RunRowsPage,
  V1Receipt,
} from '../types';

type ActionJobWire = HttpContractSuccessResponse<'tenant.action_job_detail.get'>;
type ActionJobsWire = HttpContractSuccessResponse<'tenant.action_jobs.get'>;
type ActionRunCancelWire = HttpContractSuccessResponse<'tenant.cancel_run.post'>;
type ActionRunRowsWire = HttpContractSuccessResponse<'tenant.run_rows.get'>;
type ActionRunStatusWire = HttpContractSuccessResponse<'tenant.action_run_status.get'>;
type ActionRunPublicStatusWire = ActionRunStatusWire['run']['public_status'];
type ReceiptWire = HttpContractSuccessResponse<'tenant.v1_receipt_lookup.get'>;

function mapPublicRunProgress(
  status: ActionRunPublicStatusWire,
  sheetId: number,
): RunProgress {
  return {
    runId: String(status.run_id),
    actionKind: status.action_kind || 'unknown',
    actionName: status.action_name,
    sheetId: String(sheetId),
    targetColumnId: '',
    targetRowIds: null,
    status: toRunProgressStatus(status),
    completedRows: status.completed,
    totalRows: status.total,
    failedRows: status.failed,
    costSoFar: status.cost ?? 0,
    staleReason: status.stalled_reason ?? null,
    haltedCode: status.halted_code ?? null,
    haltedReason: status.halted_reason ?? null,
    noLiveWorker: Boolean(
      status.queue && 'no_live_worker' in status.queue && status.queue.no_live_worker,
    ),
    error: status.error ?? null,
    rowErrors: status.row_errors ? {
      totalFailedRows: status.row_errors.total_failed_rows,
      groups: status.row_errors.groups.map((group) => ({
        message: group.message,
        count: group.count,
        code: group.code,
        outcome: group.outcome ?? null,
        terminal: group.terminal ?? false,
        rowIds: group.row_ids.map(String),
      })),
    } : undefined,
  };
}

function mapRunProgress(wire: ActionRunStatusWire): RunProgress {
  return mapPublicRunProgress(wire.run.public_status, wire.run.sheet_id);
}

function mapRunRows(wire: ActionRunRowsWire): RunRowsPage {
  return {
    run: {
      id: String(wire.run.id),
      actionKind: wire.run.action_kind,
      actionName: wire.run.action_name,
      status: wire.run.status,
      totalRows: wire.run.total_rows,
      completedRows: wire.run.completed_rows,
      failedRows: wire.run.failed_rows,
    },
    offset: wire.offset,
    limit: wire.limit,
    total: wire.total,
    hasMore: wire.has_more,
    nextOffset: wire.next_offset,
    rows: wire.rows.map((row) => ({
      rowId: String(row.row_id),
      rowIndex: row.row_index,
      status: row.status,
      error: row.error,
      tokensIn: row.tokens_in,
      tokensOut: row.tokens_out,
      cost: row.cost,
      retryCount: row.retry_count,
      retries: row.retries,
      cells: row.cells.map((cell) => ({
        columnId: String(cell.column_id),
        columnName: cell.column_name,
        value: cell.value,
        error: cell.error,
        tokensIn: cell.tokens_in,
        tokensOut: cell.tokens_out,
        cost: cell.cost,
        confidence: cell.confidence,
        justification: cell.justification,
        reviewState: cell.review_state ?? '',
      })),
    })),
  };
}

function mapCancelledRun(wire: ActionRunCancelWire): void {
  void wire;
  return undefined;
}

function mapReceipt(wire: ReceiptWire): V1Receipt {
  return {
    schemaVersion: wire.schema_version ?? 'frisket.receipt.v1',
    receiptId: wire.receipt_id,
    projectId: wire.project_id,
    actionId: wire.action_id,
    actionKind: wire.action_kind,
    runId: wire.run_id == null ? null : String(wire.run_id),
    opIds: wire.op_ids ?? [],
    idempotencyKey: wire.idempotency_key ?? null,
    paramsHash: wire.params_hash ?? null,
    status: wire.status,
    inputs: (wire.inputs ?? []).map((entry) => ({
      name: entry.name,
      ref: entry.ref,
    })),
    outputs: (wire.outputs ?? []).map((entry) => ({
      name: entry.name,
      ref: entry.ref,
    })),
    value: wire.value ?? null,
    providerUse: wire.provider_use ?? [],
    evidence: (wire.evidence ?? []).map((entry) => ({
      ref: entry.ref,
      retention: entry.retention ?? 'compactable',
    })),
    errors: (wire.errors ?? []).map((error) => ({
      code: error.code,
      message: error.message,
      actionKind: error.action_kind,
      field: error.field,
      details: error.details,
    })),
  };
}

function mapActionJob(wire: ActionJobWire): ActionJob {
  return {
    schemaVersion: wire.schema_version,
    projectId: wire.project_id,
    jobId: wire.job_id,
    kind: wire.kind,
    runId: wire.run_id == null ? null : String(wire.run_id),
    receiptId: wire.receipt_id ?? null,
    status: wire.status,
    actionKind: wire.action_kind ?? null,
    actionName: wire.action_name ?? null,
    attempts: wire.attempts,
    maxAttempts: wire.max_attempts,
    lease: {
      lockedBy: wire.lease.locked_by,
      lockedAt: wire.lease.locked_at,
      leaseExpiresAt: wire.lease.lease_expires_at,
      leaseExpired: wire.lease.lease_expired,
    },
    timing: {
      createdAt: wire.timing.created_at,
      startedAt: wire.timing.started_at,
      finishedAt: wire.timing.finished_at,
    },
    error: wire.error,
    resultSummary: wire.result_summary ?? {},
    progress: wire.progress ? mapPublicRunProgress(wire.progress, wire.progress.sheet_id) : null,
  };
}

function mapActionJobs(wire: ActionJobsWire): ActionJobsPage {
  return {
    schemaVersion: wire.schema_version,
    projectId: wire.project_id,
    jobs: wire.jobs.map(mapActionJob),
  };
}

export type ContractErrorFactory = (status: number, payload: unknown) => Error;

export interface RunReceiptContractOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface RunRowsContractQuery {
  offset?: number;
  limit?: number;
  status?: string;
}

export interface ActionJobsContractQuery {
  status?: string | null;
  limit?: number;
}

export function getRunProgressContract(
  projectId: string,
  runId: number,
  errorFactory: ContractErrorFactory,
  options: RunReceiptContractOptions = {},
): Promise<RunProgress> {
  return httpContract('tenant.action_run_status.get', {
    pathParams: { pid: projectId, run_id: runId },
    query: {},
    signal: options.signal,
    headers: options.headers,
    errorFactory,
  }, mapRunProgress);
}

export function getRunRowsContract(
  projectId: string,
  runId: number,
  query: RunRowsContractQuery,
  errorFactory: ContractErrorFactory,
  options: RunReceiptContractOptions = {},
): Promise<RunRowsPage> {
  return httpContract('tenant.run_rows.get', {
    pathParams: { pid: projectId, run_id: runId },
    query,
    signal: options.signal,
    headers: options.headers,
    errorFactory,
  }, mapRunRows);
}

export function cancelRunContract(
  projectId: string,
  runId: number,
  errorFactory: ContractErrorFactory,
  options: RunReceiptContractOptions = {},
): Promise<void> {
  return httpContract('tenant.cancel_run.post', {
    pathParams: { pid: projectId, run_id: runId },
    query: {},
    signal: options.signal,
    headers: options.headers,
    errorFactory,
  }, mapCancelledRun);
}

export function getReceiptContract(
  projectId: string,
  receiptId: string,
  errorFactory: ContractErrorFactory,
  options: RunReceiptContractOptions = {},
): Promise<V1Receipt> {
  return httpContract('tenant.v1_receipt_lookup.get', {
    pathParams: { pid: projectId, receipt_id: receiptId },
    query: {},
    signal: options.signal,
    headers: options.headers,
    errorFactory,
  }, mapReceipt);
}

export function listActionJobsContract(
  projectId: string,
  query: ActionJobsContractQuery,
  errorFactory: ContractErrorFactory,
  options: RunReceiptContractOptions = {},
): Promise<ActionJobsPage> {
  const { status, ...rest } = query;
  const normalizedQuery = status === null ? rest : { ...rest, status };
  return httpContract('tenant.action_jobs.get', {
    pathParams: { pid: projectId },
    query: normalizedQuery,
    signal: options.signal,
    headers: options.headers,
    errorFactory,
  }, mapActionJobs);
}

export function getActionJobContract(
  projectId: string,
  jobId: number,
  errorFactory: ContractErrorFactory,
  options: RunReceiptContractOptions = {},
): Promise<ActionJob> {
  return httpContract('tenant.action_job_detail.get', {
    pathParams: { pid: projectId, job_id: jobId },
    query: {},
    signal: options.signal,
    headers: options.headers,
    errorFactory,
  }, mapActionJob);
}
