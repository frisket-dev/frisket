import { quotedUsd } from '../actions/quotedCost';
import { UNKNOWN_COST_GATE_MESSAGE } from '../actions/model';
import type { RunEstimate } from './types';

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

// ---------------------------------------------------------------------------
// Errors

export class ApiError extends Error {
  status: number;
  /** Typed backend error code (e.g. 'embedding_source_stale'), when present, so
   *  callers can branch on it instead of matching the message string. */
  code?: string;
  /** Structured extras (e.g. {provider: 'anthropic'} for missing_provider_key,
   *  {endpoint_origin: '...'} for local endpoint failures) — see web/src/errors/remediation.ts,
   *  the one seam that turns (code, details) into user-facing remediation copy. */
  details?: Record<string, unknown>;
  constructor(
    status: number,
    message: string,
    code?: string,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

/**
 * Thrown by runAction when the server pauses for confirmation (HTTP 402,
 * ActionResult status `needs_confirmation`). Generic, not cost-only: `reason`
 * carries the server's confirmation reason (model_cost, external_metered,
 * remote_egress, destructive, irreversible_external, unknown_estimate) and
 * `estimate` is present only when a cost estimate was computed.
 */
export class ConfirmationRequiredError extends Error {
  estimate: RunEstimate;
  reason: string | null;
  constructor(estimate: RunEstimate, message: string, reason: string | null = null) {
    super(message);
    this.name = 'ConfirmationRequiredError';
    this.estimate = estimate;
    this.reason = reason;
  }
}

// ---------------------------------------------------------------------------
// Fetch helpers

function runEstimateFromUnknown(raw: unknown): RunEstimate {
  if (typeof raw === 'number' || raw === null) {
    return { cost: raw, rows: 0 };
  }
  if (raw && typeof raw === 'object') {
    return { cost: null, rows: 0, ...(raw as Partial<RunEstimate>) };
  }
  return { cost: null, rows: 0 };
}

function estimateFromDetail(raw: unknown): unknown {
  if (raw && typeof raw === 'object' && 'estimate' in raw) {
    return (raw as { estimate?: unknown }).estimate;
  }
  return undefined;
}

/** Deterministic row-count gates (e.g. derive.join's fan-out guard and
 *  derive.collection_expand's preview cap, unify-on-402)
 *  carry no priced `estimate` — their needs_confirmation envelope details are
 *  either `estimated_rows` or `preview_count`. Surface that known quantity as
 *  the RunEstimate row count (cost stays null) so the shared CostGateModal
 *  never asks for consent while withholding how many rows/items were quoted. */
export function rowsEstimateFromDetail(raw: unknown): { rows: number } | undefined {
  if (raw && typeof raw === 'object') {
    const detail = raw as { estimated_rows?: unknown; preview_count?: unknown };
    const rows = detail.estimated_rows ?? detail.preview_count;
    if (typeof rows === 'number') return { rows };
  }
  return undefined;
}

/** First argument that is a non-blank string, or '' if none qualify. Used so
 *  error surfaces never render an empty message (`??` passes '' through). */
function firstNonEmptyString(...values: Array<string | null | undefined>): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim().length > 0) return value;
  }
  return '';
}

/** Non-blank human message for a failed v1 ActionResult. Selects the first error
 *  entry that actually carries a message and falls back so a blank/absent
 *  `.message` never surfaces an
 *  empty red box. Thin wrapper over firstNonEmptyString; the single guarded
 *  idiom that replaces the blank-passthrough error coalescing at every
 *  result-message site. */
function v1ErrorMessage(
  result: { errors?: Array<{ message?: string | null }> | null } | null | undefined,
  fallback: string,
): string {
  const errors = result?.errors ?? [];
  const first = errors.find((error) => error?.message) ?? errors[0];
  return firstNonEmptyString(first?.message, fallback);
}

/** Last-resort human-readable label for a failed HTTP response with no usable
 *  server message (e.g. HTTP/2 blanks statusText). */
function httpFailureFallback(status: number): string {
  return status > 0 ? `Request failed (HTTP ${status})` : 'Request failed';
}

function apiErrorFromContract(status: number, payload: unknown): ApiError {
  const detail = isRecord(payload) ? payload.detail : undefined;
  if (isRecord(detail) && typeof detail.message === 'string') {
    const { code, message, ...details } = detail;
    return new ApiError(
      status,
      message,
      typeof code === 'string' ? code : undefined,
      Object.keys(details).length > 0 ? details : undefined,
    );
  }
  const message = typeof detail === 'string'
    ? detail
    : detail === undefined
      ? httpFailureFallback(status)
      : JSON.stringify(detail);
  return new ApiError(status, firstNonEmptyString(message, httpFailureFallback(status)));
}

// Workbench lifecycle errors historically expose only their status, message,
// and optional code. Details can contain producer-specific plugin context and
// were never part of the public workbench ApiError surface.
function workbenchPluginErrorFromContract(status: number, payload: unknown): ApiError {
  const detail = isRecord(payload) ? payload.detail : undefined;
  if (isRecord(detail) && typeof detail.message === 'string') {
    return new ApiError(
      status,
      detail.message,
      typeof detail.code === 'string' ? detail.code : undefined,
    );
  }
  const message = typeof detail === 'string'
    ? detail
    : detail === undefined
      ? httpFailureFallback(status)
      : JSON.stringify(detail);
  return new ApiError(status, firstNonEmptyString(message, httpFailureFallback(status)));
}

/** The action-run route reports domain refusals as ActionResult envelopes even
 * when their HTTP status is non-2xx. Its discriminant is product behavior,
 * not a response validator. */
function actionResultErrorFromContract(status: number, payload: unknown): Error | null {
  if (!isRecord(payload) || payload.schema_version !== 'frisket.action_result.v1') return null;
  const errors = Array.isArray(payload.errors)
    ? payload.errors.filter(isRecord)
    : [];
  if (status === 402 && payload.status === 'needs_confirmation') {
    const confirmError =
      errors.find((error) => error.field === 'params.confirmed')
      ?? errors.find((error) => isRecord(error.details) && typeof error.details.reason === 'string')
      ?? errors[0];
    const details = isRecord(confirmError?.details) ? confirmError.details : {};
    const raw =
      estimateFromDetail(details)
      ?? estimateFromDetail(confirmError?.detail)
      ?? estimateFromDetail(payload.detail)
      ?? rowsEstimateFromDetail(details);
    const estimate = runEstimateFromUnknown(raw);
    if (Array.isArray(details.claims)) {
      estimate.claims = details.claims.filter(
        (claim): claim is { field: string; display: string } =>
          isRecord(claim)
          && typeof claim.field === 'string'
          && typeof claim.display === 'string',
      );
    }
    if (typeof details.promise_set_hash === 'string') {
      estimate.promise_set_hash = details.promise_set_hash;
    }
    return new ConfirmationRequiredError(
      estimate,
      firstNonEmptyString(
        typeof confirmError?.message === 'string' ? confirmError.message : '',
        quotedUsd(estimate) === null ? UNKNOWN_COST_GATE_MESSAGE : '',
        'confirmation required',
      ),
      typeof details.reason === 'string' ? details.reason : null,
    );
  }
  if (errors.length > 0) {
    const first = errors.find((error) => typeof error.message === 'string' && error.message)
      ?? errors[0];
    return new ApiError(
      status,
      firstNonEmptyString(
        typeof first?.message === 'string' ? first.message : '',
        httpFailureFallback(status),
      ),
      typeof first?.code === 'string' ? first.code : undefined,
      isRecord(first?.details) ? first.details : undefined,
    );
  }
  return null;
}

function actionLaunchErrorFromContract(status: number, payload: unknown): Error {
  return actionResultErrorFromContract(status, payload) ?? apiErrorFromContract(status, payload);
}

/** The saved-view/saved-lens 400 envelope reads EXACTLY as the untyped
 *  transport read it: `{detail: {code, message, field}}` surfaces the message
 *  and the code and nothing else, leaving `details` undefined. The shared
 *  contract factory folds every remaining detail key into `details`; adopting
 *  that here would start populating a field this surface has never carried,
 *  so the lens path keeps its own reading and delegates the rest. */
function viewLensApiErrorFromContract(status: number, payload: unknown): ApiError {
  const detail = isRecord(payload) ? payload.detail : undefined;
  if (isRecord(detail) && typeof detail.message === 'string') {
    return new ApiError(
      status,
      detail.message,
      typeof detail.code === 'string' ? detail.code : undefined,
    );
  }
  return apiErrorFromContract(status, payload);
}

/** Estimate normally uses the shared `{detail: ...}` envelope. The backfill
 * guard is a historical bare v1 ActionError, whose code callers may inspect,
 * so preserve that one branch while delegating ordinary errors unchanged. */
function actionEstimateValidationErrorFromContract(
  status: number,
  payload: unknown,
): ApiError {
  if (
    isRecord(payload)
    && payload.schema_version === 'frisket.action_error.v1'
    && typeof payload.code === 'string'
  ) {
    return new ApiError(
      status,
      typeof payload.message === 'string'
        ? payload.message
        : httpFailureFallback(status),
      payload.code,
      isRecord(payload.details) ? payload.details : undefined,
    );
  }
  return apiErrorFromContract(status, payload);
}

/** Translate comparison historically returns a bare v1 action error for
 * validation failures. Its empty `details` object is observable, unlike the
 * normal detail-wrapped transport errors, so preserve it only for this route. */
function translateComparisonErrorFromContract(status: number, payload: unknown): ApiError {
  if (
    status === 400
    && isRecord(payload)
    && payload.schema_version === 'frisket.action_error.v1'
    && typeof payload.code === 'string'
  ) {
    return new ApiError(
      status,
      firstNonEmptyString(
        typeof payload.message === 'string' ? payload.message : undefined,
        httpFailureFallback(status),
      ),
      payload.code,
      isRecord(payload.details) ? payload.details : {},
    );
  }
  return apiErrorFromContract(status, payload);
}

/** Resolve previews have always returned bare v1 action errors from service
 * normalization. Preserve their code/details and the untyped transport's
 * ordinary-error reading while the generated contract owns request encoding. */
function resolvePreviewErrorFromContract(status: number, payload: unknown): ApiError {
  if (
    isRecord(payload)
    && payload.schema_version === 'frisket.action_error.v1'
    && typeof payload.code === 'string'
  ) {
    return new ApiError(
      status,
      firstNonEmptyString(
        typeof payload.message === 'string' ? payload.message : undefined,
        httpFailureFallback(status),
      ),
      payload.code,
      isRecord(payload.details) ? payload.details : undefined,
    );
  }
  const detail = isRecord(payload) ? payload.detail ?? payload.message : undefined;
  const detailText = typeof detail === 'string' ? detail : JSON.stringify(detail);
  return new ApiError(status, firstNonEmptyString(detailText, httpFailureFallback(status)));
}

/** Evidence reads retain their historical bare ActionError for request and
 * per-cell/link/column misses. Preserve the code and structured details that
 * the former raw transport exposed; project lookup errors remain detail-wrapped. */
function projectEvidenceErrorFromContract(
  status: number,
  payload: unknown,
): ApiError {
  if (
    isRecord(payload)
    && (payload.schema_version ?? payload.schemaVersion) === 'frisket.action_error.v1'
    && typeof payload.code === 'string'
  ) {
    return new ApiError(
      status,
      firstNonEmptyString(
        typeof payload.message === 'string' ? payload.message : undefined,
        httpFailureFallback(status),
      ),
      payload.code,
      isRecord(payload.details) ? payload.details : undefined,
    );
  }
  const detail = isRecord(payload) ? payload.detail : undefined;
  if (isRecord(detail) && typeof detail.message === 'string') {
    return new ApiError(
      status,
      detail.message,
      typeof detail.code === 'string' ? detail.code : undefined,
    );
  }
  return apiErrorFromContract(status, payload);
}

/** Preview lifecycle errors have always been a bare preview envelope, rather
 * than the generic `{detail: ...}` transport error. Preserve its 402 cost-gate
 * mapping while making every lifecycle request share the generated transport. */
function actionPreviewRunErrorFromContract(status: number, payload: unknown): Error {
  const error = isRecord(payload) && isRecord(payload.error) ? payload.error : null;
  const message = firstNonEmptyString(
    typeof error?.message === 'string' ? error.message : undefined,
    httpFailureFallback(status),
  );
  if (status === 402 && error) {
    const details = isRecord(error.details) ? error.details : {};
    const estimate = runEstimateFromUnknown(details.estimate);
    if (Array.isArray(details.claims)) {
      estimate.claims = details.claims.filter(
        (claim): claim is { field: string; display: string } =>
          isRecord(claim) &&
          typeof claim.field === 'string' &&
          typeof claim.display === 'string',
      );
    }
    if (typeof details.promise_set_hash === 'string') {
      estimate.promise_set_hash = details.promise_set_hash;
    }
    return new ConfirmationRequiredError(
      estimate,
      message,
      typeof details.reason === 'string' ? details.reason : null,
    );
  }
  return new ApiError(
    status,
    message,
    typeof error?.code === 'string' ? error.code : undefined,
  );
}

/** Embedding routes use `{detail: {code, message, ...}}`. Keep the established
 * status/message/code behavior while deliberately leaving producer extras out
 * of the general remediation-details channel. */
function embeddingErrorFromContract(status: number, payload: unknown): Error {
  const detail = isRecord(payload) ? payload.detail : undefined;
  if (isRecord(detail) && typeof detail.message === 'string') {
    return new ApiError(
      status,
      detail.message,
      typeof detail.code === 'string' ? detail.code : undefined,
    );
  }
  return apiErrorFromContract(status, payload);
}

// Runtime projection services attach producer-local `field` and
// `projection_kind` detail keys. Preserve the legacy ApiError status/message/
// code boundary without promoting those extras into browser remediation data.
function runtimeProjectionErrorFromContract(status: number, payload: unknown): ApiError {
  const detail = isRecord(payload) ? payload.detail : undefined;
  if (isRecord(detail) && typeof detail.message === 'string') {
    return new ApiError(
      status,
      detail.message,
      typeof detail.code === 'string' ? detail.code : undefined,
    );
  }
  return apiErrorFromContract(status, payload);
}

export {
  firstNonEmptyString,
  v1ErrorMessage,
  apiErrorFromContract,
  workbenchPluginErrorFromContract,
  actionLaunchErrorFromContract,
  viewLensApiErrorFromContract,
  actionEstimateValidationErrorFromContract,
  translateComparisonErrorFromContract,
  resolvePreviewErrorFromContract,
  projectEvidenceErrorFromContract,
  actionPreviewRunErrorFromContract,
  embeddingErrorFromContract,
  runtimeProjectionErrorFromContract,
};
