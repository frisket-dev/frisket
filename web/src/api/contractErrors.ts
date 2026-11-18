import { estimatedUsd } from '../actions/estimatedCost';
import { UNKNOWN_COST_GATE_MESSAGE } from '../actions/model';
import type { RunEstimate } from './types';
import { isRecord } from './wireValues';

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

function isConfirmationClaim(claim: unknown): claim is { field: string; display: string } {
  return isRecord(claim) && typeof claim.field === 'string' && typeof claim.display === 'string';
}

/** The one 402 cost-gate projection. Every fact the consent modal shows (the
 * quoted estimate, its claims, the promise-set hash the retry must echo, and
 * the confirmation reason) is copied by value from the server's details. */
function confirmationRequiredFrom(
  details: Record<string, unknown>,
  rawEstimate: unknown,
  message: (estimate: RunEstimate) => string,
): ConfirmationRequiredError {
  const estimate = runEstimateFromUnknown(rawEstimate);
  if (Array.isArray(details.claims)) {
    estimate.claims = details.claims.filter(isConfirmationClaim);
  }
  if (typeof details.promise_set_hash === 'string') {
    estimate.promise_set_hash = details.promise_set_hash;
  }
  return new ConfirmationRequiredError(
    estimate,
    message(estimate),
    typeof details.reason === 'string' ? details.reason : null,
  );
}

function recordOrUndefined(value: unknown): Record<string, unknown> | undefined {
  return isRecord(value) ? value : undefined;
}

/** `frisket.action_result.v1`: the action-run route reports domain refusals
 * as ActionResult envelopes even when their HTTP status is non-2xx. */
function actionResultError(
  status: number,
  payload: Record<string, unknown>,
): Error | null {
  const errors = Array.isArray(payload.errors) ? payload.errors.filter(isRecord) : [];
  if (status === 402 && payload.status === 'needs_confirmation') {
    const confirmError =
      errors.find((error) => error.field === 'params.confirmed')
      ?? errors.find((error) => isRecord(error.details) && typeof error.details.reason === 'string')
      ?? errors[0];
    const details = recordOrUndefined(confirmError?.details) ?? {};
    const raw =
      estimateFromDetail(details)
      ?? estimateFromDetail(confirmError?.detail)
      ?? estimateFromDetail(payload.detail)
      ?? rowsEstimateFromDetail(details);
    return confirmationRequiredFrom(details, raw, (estimate) => firstNonEmptyString(
      typeof confirmError?.message === 'string' ? confirmError.message : '',
      estimatedUsd(estimate) === null ? UNKNOWN_COST_GATE_MESSAGE : '',
      'confirmation required',
    ));
  }
  if (errors.length === 0) return null;
  const first = errors.find((error) => typeof error.message === 'string' && error.message)
    ?? errors[0];
  return new ApiError(
    status,
    firstNonEmptyString(
      typeof first?.message === 'string' ? first.message : '',
      httpFailureFallback(status),
    ),
    typeof first?.code === 'string' ? first.code : undefined,
    recordOrUndefined(first?.details),
  );
}

/** Maps a non-2xx contract response to the error the call rejects with. The
 * server emits four envelopes; the payload's shape selects the reading:
 * - `frisket.action_result.v1` (action runs), including the 402 cost gate;
 * - a bare `frisket.action_error.v1` (`{schema_version, code, message, details}`);
 * - the preview envelope `{error: {code, message, details}}`, including its 402;
 * - FastAPI's `{detail: string}` or `{detail: {code, message, ...details}}`. */
function errorFromContract(status: number, payload: unknown): Error {
  const fallback = httpFailureFallback(status);
  if (!isRecord(payload)) return new ApiError(status, fallback);

  if (payload.schema_version === 'frisket.action_result.v1') {
    const error = actionResultError(status, payload);
    if (error) return error;
  }

  if (
    (payload.schema_version ?? payload.schemaVersion) === 'frisket.action_error.v1'
    && typeof payload.code === 'string'
  ) {
    return new ApiError(
      status,
      firstNonEmptyString(typeof payload.message === 'string' ? payload.message : '', fallback),
      payload.code,
      recordOrUndefined(payload.details),
    );
  }

  if (isRecord(payload.error)) {
    const error = payload.error;
    const message = firstNonEmptyString(
      typeof error.message === 'string' ? error.message : '',
      fallback,
    );
    const details = recordOrUndefined(error.details);
    if (status === 402) {
      return confirmationRequiredFrom(details ?? {}, details?.estimate, () => message);
    }
    return new ApiError(
      status,
      message,
      typeof error.code === 'string' ? error.code : undefined,
      details,
    );
  }

  const detail = payload.detail ?? payload.message;
  if (isRecord(detail) && typeof detail.message === 'string') {
    const { code, message, ...details } = detail;
    return new ApiError(
      status,
      firstNonEmptyString(message, fallback),
      typeof code === 'string' ? code : undefined,
      Object.keys(details).length > 0 ? details : undefined,
    );
  }
  const message = typeof detail === 'string'
    ? detail
    : detail === undefined
      ? fallback
      : JSON.stringify(detail);
  return new ApiError(status, firstNonEmptyString(message, fallback));
}

export {
  firstNonEmptyString,
  v1ErrorMessage,
  errorFromContract,
};
