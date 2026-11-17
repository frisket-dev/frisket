/** Per-request wall-clock timeout ceiling (seconds). Mirrors the server-side
 * bound (ApiCallParams.timeout, contracts/actions/schemas/maps.py
 * MAX_API_CALL_TIMEOUT_SECONDS): an unbounded timeout lets one slow/hung
 * endpoint hold a worker for the whole run budget. */
export const API_CALL_MAX_TIMEOUT_SECONDS = 120;
