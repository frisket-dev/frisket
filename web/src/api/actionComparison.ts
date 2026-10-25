import type { HttpActionCompareContextRequest, HttpActionCompareContextResponse } from '../generated/openHttpContracts';
import type {
  GeneratedActionDraft,
  JsonValue,
  RegisteredActionRequest,
} from './types';
import { createActionPreviewRunsApi, type ActionPreviewStatusWire } from './actionPreviewRuns';
import { httpContract } from './httpContract';

export type ActionComparisonSource =
  | { kind: 'draft'; draft: GeneratedActionDraft }
  | { kind: 'run'; run_id: number; column_id: number };

export interface ActionComparisonContextRequest {
  source: ActionComparisonSource;
  sample_row_ids: number[];
}

// Wire shapes are generated from the backend. Only normalized draft optionals
// and preview presentation states are browser-owned projections.
export type ActionComparisonReason = NonNullable<HttpActionCompareContextResponse['reason']>;
export type ActionComparisonSourceColumn = HttpActionCompareContextResponse['source_columns'][number];
export type ActionComparisonOutputColumn = HttpActionCompareContextResponse['output_columns'][number];
export type ActionComparisonTargetColumn = HttpActionCompareContextResponse['replacement_targets'][number];
export type ActionComparisonCell = HttpActionCompareContextResponse['source_rows'][string][string];
export type ActionComparisonCellOrigin = ActionComparisonCell['origin'];
export type ActionComparisonCurrentCellValue = NonNullable<ActionComparisonCell['bounded_value']>;

export interface ActionComparisonPreviewArtifactValue {
  kind: 'preview_artifact';
  artifact_id: string;
  href: string;
  preview: string;
  value_kind: 'text' | 'json';
  total_characters: number;
  total_bytes: number;
  truncated: true;
}

export type ActionComparisonBoundedValue =
  | ActionComparisonCurrentCellValue
  | ActionComparisonPreviewArtifactValue;

export type ActionComparisonContext = Omit<HttpActionCompareContextResponse, 'draft' | 'comparison_basis'> & {
  draft: GeneratedActionDraft;
  comparison_basis: ActionComparisonSource;
};

export interface ActionComparisonPreviewRequest {
  action: RegisteredActionRequest;
  comparison: {
    basis: ActionComparisonSource;
    expected_output_signature: string;
    expected_sample_fingerprint: string;
  };
}

export interface ActionComparisonPreviewStart {
  schema_version: 'frisket.action_preview.v1';
  preview_id: string;
  total: number | null;
}

export interface ActionComparisonPreviewCell {
  state: 'value' | 'null' | 'missing' | 'error';
  value?: JsonValue;
  bounded_value?: ActionComparisonPreviewArtifactValue;
  error?: string | null;
}

export interface ActionComparisonPreviewSnapshot {
  previewId: string;
  status: 'running' | 'done' | 'error' | 'cancelled';
  progress: { done: number; total: number | null; preparation: { message: string } | null };
  rows: Record<string, Record<string, ActionComparisonPreviewCell>>;
  rowIds: number[];
  sampled: number;
  total: number | null;
  error: { code: string; message: string } | null;
  accounting: ActionPreviewStatusWire['accounting'];
}

export interface ActionComparisonRequestOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface ActionComparisonApi {
  getContext(
    request: ActionComparisonContextRequest,
    options?: ActionComparisonRequestOptions,
  ): Promise<ActionComparisonContext>;
  startPreview(
    request: ActionComparisonPreviewRequest,
    options?: ActionComparisonRequestOptions,
  ): Promise<ActionComparisonPreviewStart>;
  getPreview(
    previewId: string,
    options?: ActionComparisonRequestOptions,
  ): Promise<ActionComparisonPreviewSnapshot>;
  cancelPreview(previewId: string, options?: ActionComparisonRequestOptions): Promise<void>;
  discardPreview(previewId: string, options?: ActionComparisonRequestOptions): Promise<void>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

function normalizedDraft(raw: HttpActionCompareContextResponse['draft']): GeneratedActionDraft {
  return { action_id: raw.action_id, params: raw.params, output_names: raw.output_names,
    scope: { kind: 'sheet_rows', sheet_id: raw.scope.sheet_id,
      ...(raw.scope.row_ids == null ? {} : { row_ids: raw.scope.row_ids }) },
    ...(raw.sheet_name == null ? {} : { sheet_name: raw.sheet_name }) };
}

function draftForWire(draft: GeneratedActionDraft): HttpActionCompareContextRequest['source'] & { kind: 'draft' } {
  if (draft.scope.kind !== 'sheet_rows') throw new Error('Comparison requires a sheet-row action.');
  return { kind: 'draft', draft: { action_id: draft.action_id, params: draft.params,
    output_names: draft.output_names, scope: draft.scope,
    ...(draft.sheet_name === undefined ? {} : { sheet_name: draft.sheet_name }) } };
}

function sourceForWire(source: ActionComparisonSource): HttpActionCompareContextRequest['source'] {
  return source.kind === 'draft' ? draftForWire(source.draft) : source;
}

export function normalizeActionComparisonContext(
  wire: HttpActionCompareContextResponse,
): ActionComparisonContext {
  return {
    ...wire,
    draft: normalizedDraft(wire.draft),
    comparison_basis: wire.comparison_basis.kind === 'draft'
      ? { kind: 'draft', draft: normalizedDraft(wire.comparison_basis.draft) }
      : wire.comparison_basis,
  };
}

function previewCell(raw: unknown): ActionComparisonPreviewCell {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return { state: raw === null ? 'null' : 'value', ...(raw === null ? {} : { value: raw as JsonValue }) };
  }
  const cell = raw as Record<string, unknown>;
  const error = typeof cell.error === 'string' && cell.error ? cell.error : null;
  if (error) return { state: 'error', error };
  const bounded = cell.bounded_value;
  if (typeof bounded === 'object' && bounded !== null && !Array.isArray(bounded)) {
    const artifact = bounded as Partial<ActionComparisonPreviewArtifactValue>;
    if (
      artifact.kind === 'preview_artifact'
      && typeof artifact.artifact_id === 'string'
      && typeof artifact.href === 'string'
      && typeof artifact.preview === 'string'
      && (artifact.value_kind === 'text' || artifact.value_kind === 'json')
      && typeof artifact.total_characters === 'number'
      && typeof artifact.total_bytes === 'number'
    ) {
      return {
        state: 'value',
        value: null,
        bounded_value: { ...artifact, truncated: true } as ActionComparisonPreviewArtifactValue,
      };
    }
  }
  if (!Object.prototype.hasOwnProperty.call(cell, 'value')) return { state: 'missing' };
  if (cell.value === null || cell.value === undefined) return { state: 'null' };
  return { state: 'value', value: cell.value as JsonValue };
}

export function actionComparisonPreviewSnapshot(
  wire: ActionPreviewStatusWire,
): ActionComparisonPreviewSnapshot {
  const result = wire.result?.kind === 'row_overlay' ? wire.result : null;
  return {
    previewId: wire.preview_id,
    status: wire.status,
    progress: {
      done: wire.progress?.done ?? 0,
      total: wire.progress?.total ?? null,
      preparation: wire.progress?.preparation ?? null,
    },
    rows: Object.fromEntries(Object.entries(result?.rows ?? {}).map(
      ([rowId, cells]) => [rowId, Object.fromEntries(Object.entries(cells).map(
        ([logicalKey, cell]) => [logicalKey, previewCell(cell)],
      ))],
    )),
    rowIds: result?.row_ids ?? [],
    sampled: result?.sampled ?? 0,
    total: result?.total ?? null,
    error: wire.error ?? null,
    accounting: wire.accounting ?? null,
  };
}

export function createActionComparisonApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): ActionComparisonApi {
  const previewRuns = createActionPreviewRunsApi(errorFactory, projectId);
  return {
    getContext(request, options = {}) {
      return httpContract(
        'tenant.v1_action_compare_context.post',
        {
          pathParams: { pid: projectId }, query: {}, body: { ...request, source: sourceForWire(request.source) },
          signal: options.signal, headers: options.headers, errorFactory,
        },
        normalizeActionComparisonContext,
      );
    },
    async startPreview(request, options = {}) {
      const start = await httpContract(
        'tenant.v1_action_compare_preview_start.post',
        {
          pathParams: { pid: projectId }, query: {}, body: {
            action: { ...draftForWire(request.action).draft, idempotency_key: request.action.idempotency_key,
              ...(request.action.confirmation === undefined ? {} : { confirmation: request.action.confirmation }) },
            comparison: { ...request.comparison, basis: sourceForWire(request.comparison.basis) },
          },
          signal: options.signal, headers: options.headers, errorFactory,
        },
      );
      previewRuns.registerStarted(start.preview_id);
      return start;
    },
    async getPreview(previewId, options = {}) {
      return actionComparisonPreviewSnapshot(await previewRuns.get(previewId, options));
    },
    cancelPreview(previewId, options = {}) {
      return previewRuns.cancel(previewId, options);
    },
    async discardPreview(previewId, options = {}) {
      await httpContract(
        'tenant.v1_action_preview_cancel.delete',
        {
          pathParams: { pid: projectId, preview_id: previewId },
          query: { discard: true }, signal: options.signal, headers: options.headers, errorFactory,
        },
      );
    },
  };
}

export function actionComparisonArtifactUrl(
  projectId: string,
  previewId: string,
  artifactId: string,
): string {
  return `/api/projects/${encodeURIComponent(projectId)}/actions/v1/preview/${encodeURIComponent(previewId)}/artifacts/${encodeURIComponent(artifactId)}`;
}
