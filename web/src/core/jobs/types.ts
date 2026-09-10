// Framework-free job/epoch primitives. A `Job` is a token minted by a `JobLane` each time
// a new unit of async work supersedes whatever the lane was tracking before
// (a poll target reassignment, a one-shot fetch restart, …). Callers capture
// the token before starting async work and check `isCurrent(job)` after an
// await resolves — a false result means a newer job has since superseded
// this one and the resolution must be dropped.

export interface Job<T = unknown> {
  /** Monotonic epoch; a resolution from an older epoch is dropped. */
  readonly epoch: number;
  readonly signal: AbortSignal;
  /** Phantom marker only (never assigned) — lets a future caller parameterize
   *  what kind of result this job's resolution is expected to produce without
   *  the engine itself storing a value; keeps `T` meaningful to the type
   *  checker instead of an unused generic. */
  readonly __resultType?: T;
}

export interface JobLane {
  /** Mints a new job: bumps the epoch, aborts whatever job was previously
   *  live on this lane, and returns the fresh token. */
  start(): Job<unknown>;
  /** True iff `job` is still the lane's live job (replaces ad-hoc ref
   *  compares like `pollRunIdRef.current !== target`). */
  isCurrent(job: Job<unknown>): boolean;
  /** Retires the lane's current job with no successor: bumps the epoch (so
   *  any outstanding job's `isCurrent` now reads false) and aborts it. */
  cancel(): void;
}
