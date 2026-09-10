/** True when a request rate produces a usable finite pacing interval. */
export function isValidApiCallRate(value: unknown): value is number {
  return typeof value === 'number'
    && Number.isFinite(value)
    && value > 0
    && Number.isFinite(1 / value);
}

/** Per-request wall-clock timeout ceiling (seconds). Mirrors the server-side
 * bound (ApiCallParams.timeout, contracts/actions/schemas/maps.py
 * MAX_API_CALL_TIMEOUT_SECONDS): an unbounded timeout lets one slow/hung
 * endpoint hold a worker for the whole run budget. */
export const API_CALL_MAX_TIMEOUT_SECONDS = 120;

/** True when a timeout is a positive, finite number within the server's
 * accepted bound. */
export function isValidApiCallTimeout(value: unknown): value is number {
  return typeof value === 'number'
    && Number.isFinite(value)
    && value > 0
    && value <= API_CALL_MAX_TIMEOUT_SECONDS;
}
