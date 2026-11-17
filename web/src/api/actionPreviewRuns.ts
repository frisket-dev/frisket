import type { HttpActionPreviewRunRequest } from '../generated/openHttpContracts';
import { errorFromContract } from './contractErrors';
import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import { resolveRunActionInvocation } from './v1ActionSession';
import type {
  ActionExecutionRequest,
  CellValue,
  PreviewOverlayCell,
  PreviewOverlayColumn,
  PreviewSampleResult,
  PreviewStartResult,
} from './types';
import { isDeriveCompositeRequest } from './types';
import { parsePreviewTemporalValue } from '../temporal/preview';
import { cellValueFromWire } from './wireValues';

interface ActionPreviewRunOptions {
  projectId?: string;
  signal?: AbortSignal;
}

type ActionPreviewRunRequest = HttpActionPreviewRunRequest;
export type ActionPreviewStartWire =
  HttpContractSuccessResponse<'tenant.v1_action_preview_start.post'>;
export type ActionPreviewStatusWire =
  HttpContractSuccessResponse<'tenant.v1_action_preview_status.get'>;

export function createActionPreviewRunsApi(
  defaultProjectId: string,
) {
  const projectsByPreviewId = new Map<string, string>();
  const previewNotFound = () => errorFromContract(400, {
    schema_version: 'frisket.action_preview.v1',
    error: {
      schema_version: 'frisket.action_error.v1',
      code: 'preview_not_found',
      message: 'Preview run was not found for this project.',
      action_kind: null,
      field: null,
      details: {},
      needs_confirmation: false,
    },
  });

  return {
    registerStarted(previewId: string): void {
      projectsByPreviewId.set(previewId, defaultProjectId);
    },
    async start(
      action: ActionPreviewRunRequest,
      options: ActionPreviewRunOptions = {},
    ): Promise<ActionPreviewStartWire> {
      const projectId = options.projectId ?? defaultProjectId;
      const start = await httpContract(
        'tenant.v1_action_preview_start.post',
        {
          pathParams: { pid: projectId },
          body: action,
          signal: options.signal,
        },
      );
      projectsByPreviewId.set(start.preview_id, projectId);
      return start;
    },

    async get(
      previewId: string,
      options: ActionPreviewRunOptions = {},
    ): Promise<ActionPreviewStatusWire> {
      const projectId = projectsByPreviewId.get(previewId);
      if (!projectId) throw previewNotFound();
      const preview = await httpContract(
        'tenant.v1_action_preview_status.get',
        {
          pathParams: { pid: projectId, preview_id: previewId },
          signal: options.signal,
        },
      );
      if (preview.status !== 'running') projectsByPreviewId.delete(previewId);
      return preview;
    },

    async cancel(previewId: string, options: ActionPreviewRunOptions = {}): Promise<void> {
      const projectId = projectsByPreviewId.get(previewId);
      if (!projectId) throw previewNotFound();
      await httpContract(
        'tenant.v1_action_preview_cancel.delete',
        {
          pathParams: { pid: projectId, preview_id: previewId },
          signal: options.signal,
        },
        () => undefined,
      );
      projectsByPreviewId.delete(previewId);
    },
  };
}

type WirePreviewColumn = NonNullable<ActionPreviewStatusWire['result']>['columns'][number];

function previewOverlayColumn(wire: WirePreviewColumn): PreviewOverlayColumn {
  return {
    name: wire.name,
    columnType: wire.column_type,
    format: wire.format ?? null,
    hidden: wire.hidden ?? false,
    overwritesColumnId:
      wire.overwrites_column_id == null ? null : String(wire.overwrites_column_id),
  };
}

function previewCellValue(value: unknown): CellValue {
  if (parsePreviewTemporalValue(value)) return value as Record<string, unknown>;
  return cellValueFromWire(value);
}

function previewOverlayCell(cell: Record<string, unknown>): PreviewOverlayCell {
  return {
    value: previewCellValue(cell.value),
    error: typeof cell.error === 'string' ? cell.error : null,
    confidence:
      typeof cell.confidence === 'number' && Number.isFinite(cell.confidence)
        ? cell.confidence
        : null,
    justification: typeof cell.justification === 'string' ? cell.justification : null,
    outcome: typeof cell.outcome === 'string' ? cell.outcome : null,
  };
}

function previewSampleResult(wire: ActionPreviewStatusWire): PreviewSampleResult {
  const status = (['running', 'done', 'error', 'cancelled'].includes(wire.status)
    ? wire.status
    : 'running') as PreviewSampleResult['status'];
  const result = wire.result ?? null;
  const mapCells = (fields: Record<string, Record<string, unknown>>) => Object.fromEntries(
    Object.entries(fields).map(([field, cell]) => [field, previewOverlayCell(cell)]),
  );
  const common = {
    previewId: wire.preview_id,
    accounting: wire.accounting ?? null,
    status,
    progress: {
      done: wire.progress?.done ?? 0,
      total: wire.progress?.total ?? null,
      preparation: wire.progress?.preparation ?? null,
    },
    columns: (result?.columns ?? []).map(previewOverlayColumn),
    sampled: result?.sampled ?? 0,
    total: result?.total ?? null,
    error:
      wire.error && typeof wire.error.message === 'string'
        ? {
            code: typeof wire.error.code === 'string' ? wire.error.code : 'preview_failed',
            message: wire.error.message,
          }
        : null,
  };
  if (result?.kind === 'table') return {
    ...common, kind: 'table', rows: result.rows.map(mapCells),
    warnings: result.warnings ?? [],
  };
  return {
    ...common, kind: 'row_overlay',
    sheetId: result?.sheet_id != null ? String(result.sheet_id) : null,
    rowIds: (result?.row_ids ?? []).map(Number),
    rows: Object.fromEntries(Object.entries(result?.rows ?? {}).map(
      ([rowId, fields]) => [rowId, mapCells(fields)],
    )),
  };
}

/** Owns preview translation, lifecycle binding, and preview-specific
 * wire-to-public mapping for one project. */
export function createActionPreviewApi(
  projectId: string,
) {
  const previewRuns = createActionPreviewRunsApi(projectId);

  return {
    /** Bind a preview started by another route (scratch comparisons) to this
     * project so its status and cancel requests resolve. */
    registerStarted(start: ActionPreviewStartWire): PreviewStartResult {
      previewRuns.registerStarted(start.preview_id);
      return { previewId: start.preview_id, total: start.total };
    },

    /** Start an in-memory action preview: computes a small sample server-side and
     *  returns a job id to poll. Nothing is persisted (no op/run/column/cell). */
    async startPreview(request: ActionExecutionRequest): Promise<PreviewStartResult> {
      const invocation = resolveRunActionInvocation(undefined, projectId);
      if (isDeriveCompositeRequest(request)) request = request.extraction;
      const start = await previewRuns.start(
        request as unknown as ActionPreviewRunRequest,
        invocation,
      );
      return { previewId: start.preview_id, total: start.total };
    },

    /** Poll a preview job's status; once `status === 'done'` the sample rows/columns
     *  are on the result for the grid overlay. */
    async getPreview(previewId: string): Promise<PreviewSampleResult> {
      return previewSampleResult(await previewRuns.get(previewId));
    },

    /** Cancel a preview job (idempotent): stops server compute and drops the job. */
    async cancelPreview(previewId: string): Promise<void> {
      await previewRuns.cancel(previewId);
    },
  };
}
