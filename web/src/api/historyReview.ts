import { ApiError, v1ErrorMessage } from './contractErrors';
import type { GeneratedActionParams } from '../generated/actionTypes';
import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import {
  resolveRunActionInvocation,
  type ResolvedRunActionInvocation,
  type V1ActionSession,
} from './v1ActionSession';
import type {
  CellValue,
  ColumnRun,
  ColumnRunsInfo,
  ColumnType,
  HistoryOp,
  HistoryOpRun,
  HistoryState,
  ReviewAction,
  ReviewBundleField,
  ReviewBundlePage,
  RunRowErrorSummary,
} from './types';

export interface HistoryReviewOptions {
  projectId?: string;
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export type ColumnRunsWire =
  HttpContractSuccessResponse<'tenant.column_runs.get'>;
export type HistoryWire = HttpContractSuccessResponse<'tenant.history.get'>;
export type ReviewBundlesWire =
  HttpContractSuccessResponse<'tenant.review_bundles_ep.get'>;
export type ReviewCountWire =
  HttpContractSuccessResponse<'tenant.review_count_ep.get'>;

export interface HistoryReviewApi {
  getColumnRuns(
    columnId: string,
    offset?: number,
    limit?: number,
    options?: HistoryReviewOptions,
  ): Promise<ColumnRunsWire>;
  getHistory(
    offset?: number | null,
    limit?: number,
    options?: HistoryReviewOptions,
  ): Promise<HistoryWire>;
  getReviewBundles(
    offset?: number,
    limit?: number,
    runId?: string,
    includeReviewed?: boolean,
    options?: HistoryReviewOptions,
  ): Promise<ReviewBundlesWire>;
  getReviewCount(runId?: string, options?: HistoryReviewOptions): Promise<ReviewCountWire>;
}

/** The feature-facing history and review port: mapped reads plus the cursor
 * and review-queue mutations. real.ts composes this with the column-run cache
 * enrichment it still owns through the action-runs owner. */
export interface HistoryReviewDomainApi {
  getColumnRuns(
    columnId: string,
    offset?: number,
    limit?: number,
  ): Promise<ColumnRunsInfo>;
  getHistory(
    offset?: number | null,
    limit?: number,
    options?: HistoryReviewOptions,
  ): Promise<HistoryState>;
  getReviewBundles(
    offset?: number,
    limit?: number,
    runId?: string,
    includeReviewed?: boolean,
  ): Promise<ReviewBundlePage>;
  getReviewCount(runId?: string): Promise<number>;
  undo(): Promise<HistoryState>;
  redo(): Promise<HistoryState>;
  stepTo(opIndex: number): Promise<HistoryState>;
  reviewItem(
    itemId: string,
    action: ReviewAction,
    editedValue?: CellValue,
    note?: string | null,
  ): Promise<void>;
}

/** RealApi owns the one browser session; this domain receives only its narrow
 * V1 action surface for history and review mutations. It never mints its own
 * idempotency keys or project binding. */
export interface HistoryReviewDependencies {
  v1ActionSession: Pick<
    V1ActionSession,
    | 'registeredProjectActionSpec'
    | 'withV1ActionResult'
    | 'assertCompletedV1ActionResult'
  >;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type OperationActionId = 'operation.undo' | 'operation.redo';
type OperationStepActionParams = GeneratedActionParams[OperationActionId];
type ReviewDecisionActionParams = GeneratedActionParams['review.decision'];

export function createHistoryReviewApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): HistoryReviewApi {
  return {
    getColumnRuns(columnId, offset = 0, limit = 20, options = {}) {
      return httpContract(
        'tenant.column_runs.get',
        {
          pathParams: {
            pid: projectId,
            // FrisketApi's long-standing column reference is a string. The
            // route's OpenAPI path parameter is numeric, so keep the existing
            // browser bytes while using the generated operation signature.
            column_id: columnId as unknown as number,
          },
          query: { offset, limit },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    getHistory(offset, limit = 50, options = {}) {
      return httpContract(
        'tenant.history.get',
        {
          pathParams: { pid: options.projectId ?? projectId },
          query: { offset: offset ?? undefined, limit },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    getReviewBundles(offset = 0, limit = 25, runId, includeReviewed = false, options = {}) {
      return httpContract(
        'tenant.review_bundles_ep.get',
        {
          pathParams: { pid: projectId },
          query: {
            offset,
            limit,
            run_id: runId === undefined ? undefined : Number(runId),
            include_reviewed: includeReviewed || undefined,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    getReviewCount(runId, options = {}) {
      return httpContract(
        'tenant.review_count_ep.get',
        {
          pathParams: { pid: projectId },
          query: { run_id: runId === undefined ? undefined : Number(runId) },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}

type WireColumnRun = ColumnRunsWire['runs'][number];
type WireHistoryRun = HistoryWire['ops'][number]['run'];
type WireRunRowErrorSummary = NonNullable<NonNullable<WireHistoryRun>['row_errors']>;

const HISTORY_KINDS: ReadonlySet<HistoryOp['kind']> = new Set([
  'import', 'map', 'derive', 'edit', 'review-batch', 'sort',
] as HistoryOp['kind'][]);

const toHistoryKind = (kind: string): HistoryOp['kind'] =>
  (HISTORY_KINDS.has(kind as HistoryOp['kind']) ? kind : 'edit') as HistoryOp['kind'];

/** sqlite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS", UTC) → ISO. */
const toHistoryIso = (timestamp: string): string =>
  timestamp.includes('T') ? timestamp : `${timestamp.replace(' ', 'T')}Z`;

function actionDisplayNameFromActionKind(actionKind: string): string {
  const normalized = actionKind.replace(/[._-]+/g, ' ').trim();
  if (!normalized) return 'AI run';
  return normalized
    .split(/\s+/)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

/** Backend values are real JSON. Legacy structured renderers still receive
 * JSON strings; source-bound temporal values keep their typed object shape. */
function toReviewCellValue(value: unknown): CellValue {
  if (value === null || value === undefined) return null;
  if (
    typeof value === 'string' ||
    typeof value === 'number' ||
    typeof value === 'boolean'
  ) {
    return value;
  }
  if (
    typeof value === 'object' &&
    !Array.isArray(value) &&
    typeof (value as { schema_version?: unknown }).schema_version === 'string' &&
    [
      'frisket.timeline_point.v1',
      'frisket.timeline_points.v1',
      'frisket.timeline_range.v1',
      'frisket.timeline_ranges.v1',
    ].includes((value as { schema_version: string }).schema_version)
  ) {
    return value as Record<string, unknown>;
  }
  return JSON.stringify(value);
}

function mapRunRowErrors(
  wire: WireRunRowErrorSummary | null | undefined,
): RunRowErrorSummary | undefined {
  if (!wire) return undefined;
  return {
    totalFailedRows: wire.total_failed_rows,
    groups: wire.groups.map((group) => ({
      message: group.message,
      count: group.count,
      code: group.code,
      outcome: group.outcome ?? null,
      terminal: group.terminal ?? false,
      rowIds: group.row_ids.map((id) => String(id)),
    })),
  };
}

function mapColumnRun(wire: WireColumnRun): ColumnRun {
  return {
    runId: String(wire.run_id),
    actionKind: wire.action_kind,
    actionName: wire.action_name || actionDisplayNameFromActionKind(wire.action_kind),
    model: wire.model ?? '',
    status: wire.status,
    spec: wire.spec ?? {},
    totalRows: wire.total_rows,
    completedRows: wire.completed_rows,
    failedRows: wire.failed_rows,
    cost: wire.cost_actual,
    startedAt: wire.started_at ? toHistoryIso(wire.started_at) : null,
    finishedAt: wire.finished_at ? toHistoryIso(wire.finished_at) : null,
    durationMs: wire.duration_ms ?? null,
    tokensIn: wire.tokens_in ?? null,
    tokensOut: wire.tokens_out ?? null,
    current: wire.current,
    humanScore: {
      passed: wire.human_score.passed,
      graded: wire.human_score.graded,
    },
    judgeScores: wire.judge_scores.map((score) => ({
      runId: String(score.run_id),
      model: score.model ?? null,
      startedAt: score.started_at,
      verdictColumnId: String(score.verdict_column_id),
      passed: score.passed,
      graded: score.graded,
      compared: score.compared,
      disagreementCount: score.disagreement_count ?? null,
    })),
  };
}

function mapColumnRuns(wire: ColumnRunsWire): ColumnRunsInfo {
  return {
    columnName: wire.column.name,
    offset: wire.offset,
    limit: wire.limit,
    totalRuns: wire.total,
    hasMore: wire.has_more,
    nextOffset: wire.next_offset,
    currentRun: wire.current_run ? mapColumnRun(wire.current_run) : null,
    currentRunLoaded: wire.current_run_loaded,
    latestRun: wire.latest_run ? mapColumnRun(wire.latest_run) : null,
    latestRunLoaded: wire.latest_run_loaded,
    mixedOrigins: wire.column.mixed_origins,
    runs: wire.runs.map(mapColumnRun),
  };
}

function mapHistoryRun(run: WireHistoryRun): HistoryOpRun | undefined {
  if (!run) return undefined;
  return {
    runId: String(run.run_id),
    status: run.status,
    actionKind: run.action_kind,
    model: run.model,
    params: run.params,
    totalRows: run.total_rows,
    completedRows: run.completed_rows,
    failedRows: run.failed_rows,
    costEstimate: run.cost_estimate ?? undefined,
    costActual: run.cost_actual,
    startedAt: toHistoryIso(run.started_at),
    finishedAt: run.finished_at ? toHistoryIso(run.finished_at) : null,
    workerVersion: run.worker_version,
    outputColumns: run.output_columns.map((column) => ({
      id: String(column.id),
      name: column.name,
    })),
    rowErrors: mapRunRowErrors(run.row_errors),
  };
}

function mapHistoryOp(wire: HistoryWire['ops'][number]): HistoryOp {
  return {
    id: String(wire.id),
    index: wire.index,
    label: wire.label ?? wire.kind,
    kind: toHistoryKind(wire.kind),
    at: toHistoryIso(wire.created_at),
    rowsAffected: wire.run?.total_rows ?? 0,
    cost: wire.run?.cost_actual,
    run: mapHistoryRun(wire.run),
    ...(wire.barrier ? { barrier: true } : {}),
  };
}

function mapHistory(wire: HistoryWire): HistoryState {
  const mapTarget = (target: HistoryWire['undo_target']) => target
    ? { id: String(target.id), index: target.index, barrier: target.barrier }
    : null;
  return {
    ops: wire.ops.map(mapHistoryOp),
    offset: wire.offset,
    limit: wire.limit,
    total: wire.total,
    hasMoreBefore: wire.has_more_before,
    hasMoreAfter: wire.has_more_after,
    prevOffset: wire.prev_offset,
    nextOffset: wire.next_offset,
    cursorIndex: wire.cursor_index,
    cursorOp: wire.cursor_op ? mapHistoryOp(wire.cursor_op) : null,
    cursorOpLoaded: wire.cursor_op_loaded,
    undoTarget: mapTarget(wire.undo_target),
    redoTarget: mapTarget(wire.redo_target),
    revision: {
      total: wire.revision.total,
      maxOpId: wire.revision.max_op_id > 0 ? String(wire.revision.max_op_id) : null,
      opCursor: wire.revision.op_cursor > 0 ? String(wire.revision.op_cursor) : null,
    },
  };
}

function mapReviewBundleField(
  wire: ReviewBundlesWire['bundles'][number]['fields'][number],
): ReviewBundleField {
  return {
    id: `${wire.run_id}:${wire.row_id}:${wire.column_id}`,
    runId: String(wire.run_id),
    sheetId: String(wire.sheet_id),
    rowId: String(wire.row_id),
    columnId: String(wire.column_id),
    columnName: wire.column_name,
    columnType: (wire.column_type as ColumnType | undefined) ?? 'text',
    value: toReviewCellValue(wire.value),
    confidence: wire.confidence ?? 0,
    justification: wire.justification ?? '',
    reviewDecision: wire.review_decision ?? null,
    note: wire.review_note ?? null,
    reviewState: wire.review_state ?? 'unreviewed',
    role: wire.role,
    chore: wire.chore,
  };
}

function mapReviewBundles(wire: ReviewBundlesWire): ReviewBundlePage {
  if (wire.bundles.length === 0) {
    return {
      schemaVersion: wire.schema_version,
      bundles: [],
      offset: wire.offset,
      limit: wire.limit,
      total: wire.total,
      hasMore: wire.has_more,
      nextOffset: wire.next_offset,
    };
  }
  const bundles = wire.bundles.map((bundle) => {
    const source = Object.fromEntries(
      Object.entries(bundle.source ?? {}).map(([key, value]) => [
        key,
        toReviewCellValue(value),
      ]),
    );
    return {
      id: bundle.id,
      runId: String(bundle.run_id),
      sheetId: String(bundle.sheet_id),
      sheetName: bundle.sheet_name ?? `sheet ${bundle.sheet_id}`,
      rowId: String(bundle.row_id),
      rowIndex: bundle.row_id - 1,
      actionKind: bundle.action_kind,
      actionName: bundle.action_name || actionDisplayNameFromActionKind(bundle.action_kind),
      model: bundle.model ?? '',
      confidence: bundle.confidence ?? 0,
      source,
      context: Object.entries(source)
        .map(([key, value]) => `${key}: ${String(value)}`)
        .join('  ·  ')
        .slice(0, 400),
      fields: bundle.fields.map(mapReviewBundleField),
      evidence: bundle.evidence.map(mapReviewBundleField),
    };
  });
  return {
    schemaVersion: wire.schema_version,
    bundles,
    offset: wire.offset,
    limit: wire.limit,
    total: wire.total,
    hasMore: wire.has_more,
    nextOffset: wire.next_offset,
  };
}

/** The cursor walk is bounded: the backend only exposes single-step operation
 * controls, so a stepTo() that never reaches its target stops rather than
 * posting forever. */
const MAX_HISTORY_WALK_STEPS = 100;

/** Outcomes the server reports for a step that simply did not happen (the
 * cursor moved underneath us, the operation is gone, or it is irreversible).
 * These leave the cursor where it is instead of failing the caller. */
const NON_MOVING_STEP_CODES: ReadonlySet<string> = new Set([
  'operation_unavailable',
  'operation_mismatch',
  'irreversible_barrier',
]);

export function createHistoryReviewDomainApi(
  errorFactory: ContractErrorFactory,
  dependencies: HistoryReviewDependencies,
  projectId: string,
): HistoryReviewDomainApi {
  const transport = createHistoryReviewApi(errorFactory, projectId);
  const { v1ActionSession } = dependencies;

  /** Every step of one mutation uses the same explicit invocation. */
  const historyForInvocation = (
    invocation: ResolvedRunActionInvocation,
    offset?: number | null,
    limit = 50,
  ): Promise<HistoryState> =>
    transport
      .getHistory(offset, limit, {
        projectId: invocation.projectId,
        signal: invocation.signal,
      })
      .then(mapHistory);

  const runOperationStepAction = async (
    kind: OperationActionId,
    expectedOpId: string | null | undefined,
    invocation: ResolvedRunActionInvocation,
  ): Promise<boolean> => {
    const params: OperationStepActionParams = {};
    if (expectedOpId != null) {
      const opId = Number(expectedOpId);
      if (!Number.isInteger(opId) || opId <= 0) {
        throw new ApiError(400, `Operation ${expectedOpId} is not a valid v1 operation ref`);
      }
      params.expected_op_id = opId;
    }
    const spec = v1ActionSession.registeredProjectActionSpec(
      kind,
      params,
      invocation.projectId,
      invocation.signal,
    );
    return v1ActionSession.withV1ActionResult(
      spec,
      (out) => {
        if (out.status === 'completed') {
          return true;
        }
        const code = out.errors[0]?.code;
        if (code != null && NON_MOVING_STEP_CODES.has(code)) {
          console.warn(`${kind} blocked:`, v1ErrorMessage(out, code));
          return false;
        }
        throw new ApiError(400, v1ErrorMessage(out, `${kind} failed`));
      },
      { postMode: 'operation', operationInvocation: invocation },
    );
  };

  const walkHistoryTo = async (
    opIndex: number,
    hist: HistoryState,
    invocation: ResolvedRunActionInvocation,
    guard = 0,
  ): Promise<HistoryState> => {
    if (hist.cursorIndex === opIndex || guard >= MAX_HISTORY_WALK_STEPS) return hist;
    const target = hist.cursorIndex > opIndex ? hist.undoTarget : hist.redoTarget;
    if (!target) return hist;
    const moved = hist.cursorIndex > opIndex
      ? await runOperationStepAction('operation.undo', target.id, invocation)
      : await runOperationStepAction('operation.redo', target.id, invocation);
    if (!moved) return hist;
    return walkHistoryTo(
      opIndex,
      await historyForInvocation(invocation, null, hist.limit),
      invocation,
      guard + 1,
    );
  };

  return {
    async getColumnRuns(columnId, offset = 0, limit = 20) {
      return mapColumnRuns(await transport.getColumnRuns(columnId, offset, limit));
    },

    async getHistory(offset, limit = 50, options) {
      return mapHistory(await transport.getHistory(offset, limit, options));
    },

    async getReviewBundles(offset = 0, limit = 25, runId, includeReviewed = false) {
      return mapReviewBundles(await transport.getReviewBundles(offset, limit, runId, includeReviewed));
    },

    async getReviewCount(runId) {
      const wire = await transport.getReviewCount(runId);
      return Number(wire.count ?? 0);
    },

    async undo() {
      const invocation = resolveRunActionInvocation(undefined, projectId);
      const hist = await historyForInvocation(invocation);
      await runOperationStepAction('operation.undo', hist.undoTarget?.id ?? null, invocation);
      return historyForInvocation(invocation);
    },

    async redo() {
      const invocation = resolveRunActionInvocation(undefined, projectId);
      const hist = await historyForInvocation(invocation);
      await runOperationStepAction('operation.redo', hist.redoTarget?.id ?? null, invocation);
      return historyForInvocation(invocation);
    },

    async stepTo(opIndex) {
      const invocation = resolveRunActionInvocation(undefined, projectId);
      return walkHistoryTo(
        opIndex,
        await historyForInvocation(invocation),
        invocation,
      );
    },

    async reviewItem(itemId, action, editedValue, note) {
      const itemRef = itemId.split(':');
      const [runId, rowId, colId] = itemRef.map(Number);
      if (
        itemRef.length !== 3 ||
        ![runId, rowId, colId].every((id) => Number.isInteger(id) && id > 0)
      ) {
        throw new ApiError(400, `Review item ${itemId} is not a valid v1 result-cell ref`);
      }
      const params: ReviewDecisionActionParams = {
        run_id: runId,
        row_id: rowId,
        column_id: colId,
        decision: action,
        // An edit carries its value (null when cleared); the two rejection
        // decisions name their persistence semantics themselves.
        ...(action === 'edit'
          ? { value: (editedValue ?? null) as ReviewDecisionActionParams['value'] }
          : {}),
        ...(note !== undefined ? { note } : {}),
      };
      const spec = v1ActionSession.registeredProjectActionSpec('review.decision', params);
      await v1ActionSession.withV1ActionResult(spec, (out) => {
        v1ActionSession.assertCompletedV1ActionResult(out, 'Review decision failed');
      });
    },
  };
}
