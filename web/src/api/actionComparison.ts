import type { HttpContractOperationId } from '../generated/openHttpContracts';
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

export interface ActionComparisonReason {
  code: string;
  message: string;
}

export interface ActionComparisonSourceColumn {
  param: string;
  column_id: number;
  name: string;
  column_type: string;
  format: string | null;
}

export interface ActionComparisonOutputColumn {
  logical_key: string;
  column_id: number | null;
  current_name: string;
  column_type: string;
  format: string | null;
  hidden: boolean;
  schema: JsonValue;
}

export interface ActionComparisonTargetColumn {
  logical_key: string;
  current_name: string;
  column_id: number;
}

export interface ActionComparisonCellOrigin {
  kind: 'source_cell' | 'run_result' | 'manual_edit' | 'missing';
  run_id: number | null;
  op_id: number | null;
  review_decision: 'reject_clear' | 'edit' | null;
}

export interface ActionComparisonCurrentCellValue {
  kind: 'current_cell';
  preview: string;
  value_kind: 'text' | 'json' | 'bigint' | 'legacy_invalid';
  total_characters: number;
  total_bytes: number;
  retrieval_revision: number;
  truncated: true;
  row_id: number;
  column_id: number;
}

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

/** A current source/baseline cell. Explicit states keep null, missing and
 * failures distinct all the way to the comparison table. */
export interface ActionComparisonCell {
  state: 'value' | 'null' | 'missing' | 'error';
  value?: JsonValue;
  bounded_value?: ActionComparisonCurrentCellValue;
  origin: ActionComparisonCellOrigin;
  error?: string | null;
}

export interface ActionComparisonContext {
  schema_version: 'frisket.action_compare_context.v1';
  available: boolean;
  reason: ActionComparisonReason | null;
  limits: {
    max_sample_rows: number;
    max_challengers: number;
  };
  baseline_mode: 'preview' | 'current_values';
  comparison_basis: ActionComparisonSource;
  draft: GeneratedActionDraft;
  sheet: { id: number; name: string };
  sample_row_ids: number[];
  sample_fingerprint: string;
  locked_params: string[];
  source_columns: ActionComparisonSourceColumn[];
  /** Row id -> source column name -> current cell. */
  source_rows: Record<string, Record<string, ActionComparisonCell>>;
  output_columns: ActionComparisonOutputColumn[];
  expected_output_signature: string;
  replacement_targets: ActionComparisonTargetColumn[];
  /** Present only when A is the current table value. */
  baseline_rows: Record<string, Record<string, ActionComparisonCell>> | null;
  origin_summary: {
    mixed_runs: boolean;
    includes_manual_edits: boolean;
    includes_reject_clear: boolean;
  };
}

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

/** These endpoint IDs are generated by the backend contract change. Keeping
 * the temporary cast here lets this isolated frontend commit typecheck against
 * its exact base; integration removes the cast once generated contracts land. */
type ComparisonContract = <Response>(
  endpointId: string,
  options: {
    pathParams: Record<string, string>;
    query: Record<string, unknown>;
    body?: unknown;
    signal?: AbortSignal;
    headers?: HeadersInit;
    errorFactory: ContractErrorFactory;
  },
  mapResponse?: (wire: Response) => Response,
) => Promise<Response>;

const comparisonContract = httpContract as unknown as ComparisonContract;

function normalizedDraft(raw: GeneratedActionDraft): GeneratedActionDraft {
  const scope = { ...raw.scope };
  if (scope.kind === 'sheet_rows' && scope.row_ids == null) delete scope.row_ids;
  const draft = { ...raw, scope };
  if (draft.sheet_name == null) delete draft.sheet_name;
  return draft;
}

export function normalizeActionComparisonContext(
  wire: ActionComparisonContext,
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
      return comparisonContract<ActionComparisonContext>(
        'tenant.v1_action_compare_context.post',
        {
          pathParams: { pid: projectId }, query: {}, body: request,
          signal: options.signal, headers: options.headers, errorFactory,
        },
        normalizeActionComparisonContext,
      );
    },
    async startPreview(request, options = {}) {
      const start = await comparisonContract<ActionComparisonPreviewStart>(
        'tenant.v1_action_compare_preview_start.post',
        {
          pathParams: { pid: projectId }, query: {}, body: request,
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
    discardPreview(previewId, options = {}) {
      return comparisonContract<void>(
        'tenant.v1_action_preview_cancel.delete' satisfies HttpContractOperationId,
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
