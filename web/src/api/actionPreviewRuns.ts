import type { HttpActionPreviewRunRequest } from '../generated/openHttpContracts';
import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import { type ResolvedRunActionInvocation } from './v1ActionSession';
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

export interface ActionPreviewRunOptions {
  projectId?: string;
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export type ActionPreviewRunRequest = HttpActionPreviewRunRequest;
export type ActionPreviewStartWire =
  HttpContractSuccessResponse<'tenant.v1_action_preview_start.post'>;
export type ActionPreviewStatusWire =
  HttpContractSuccessResponse<'tenant.v1_action_preview_status.get'>;

export interface ActionPreviewRunsApi {
  registerStarted(previewId: string): void;
  start(
    action: ActionPreviewRunRequest,
    options?: ActionPreviewRunOptions,
  ): Promise<ActionPreviewStartWire>;
  get(
    previewId: string,
    options?: ActionPreviewRunOptions,
  ): Promise<ActionPreviewStatusWire>;
  cancel(previewId: string, options?: ActionPreviewRunOptions): Promise<void>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

export interface ActionPreviewDomainDependencies {
  errorFactory: ContractErrorFactory;
}

export interface ActionPreviewDomainApi {
  registerStarted(start: ActionPreviewStartWire): PreviewStartResult;
  start(
    request: ActionExecutionRequest,
    invocation: ResolvedRunActionInvocation,
  ): Promise<PreviewStartResult>;
  get(previewId: string): Promise<PreviewSampleResult>;
  cancel(previewId: string): Promise<void>;
}

export function createActionPreviewRunsApi(
  errorFactory: ContractErrorFactory,
  defaultProjectId: string,
): ActionPreviewRunsApi {
  const projectsByPreviewId = new Map<string, string>();
  const previewNotFound = () => errorFactory(400, {
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
    registerStarted(previewId) {
      projectsByPreviewId.set(previewId, defaultProjectId);
    },
    async start(action, options = {}) {
      const projectId = options.projectId ?? defaultProjectId;
      const start = await httpContract(
        'tenant.v1_action_preview_start.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: action,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
      projectsByPreviewId.set(start.preview_id, projectId);
      return start;
    },

    async get(previewId, options = {}) {
      const projectId = projectsByPreviewId.get(previewId);
      if (!projectId) throw previewNotFound();
      const preview = await httpContract(
        'tenant.v1_action_preview_status.get',
        {
          pathParams: { pid: projectId, preview_id: previewId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
      if (preview.status !== 'running') projectsByPreviewId.delete(previewId);
      return preview;
    },

    async cancel(previewId, options = {}) {
      const projectId = projectsByPreviewId.get(previewId);
      if (!projectId) throw previewNotFound();
      await httpContract(
        'tenant.v1_action_preview_cancel.delete',
        {
          pathParams: { pid: projectId, preview_id: previewId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
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
  if (value === null || value === undefined) return null;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return value;
  }
  if (parsePreviewTemporalValue(value)) return value as Record<string, unknown>;
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

/** RealApi owns the shared action session; this domain owns preview translation,
 * lifecycle binding, and preview-specific wire-to-public mapping. */
export function createActionPreviewDomainApi({
  errorFactory,
}: ActionPreviewDomainDependencies, projectId: string): ActionPreviewDomainApi {
  const previewRuns = createActionPreviewRunsApi(errorFactory, projectId);

  return {
    registerStarted(start) {
      previewRuns.registerStarted(start.preview_id);
      return { previewId: start.preview_id, total: start.total };
    },
    async start(request, invocation) {
      if (isDeriveCompositeRequest(request)) request = request.extraction;
      const start = await previewRuns.start(
        request as unknown as ActionPreviewRunRequest,
        invocation,
      );
      return { previewId: start.preview_id, total: start.total };
    },

    async get(previewId) {
      return previewSampleResult(await previewRuns.get(previewId));
    },

    async cancel(previewId) {
      await previewRuns.cancel(previewId);
    },
  };
}
