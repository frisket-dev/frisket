// The per-project action-job resource. It owns the run/queued/dock schedulers,
// request epochs, terminal-progress cache, and completed action feedback. React
// bind/components only activate this lifecycle and invoke its domain commands.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';
import { createJobLane } from '../core/jobs/engine';
import type { Job } from '../core/jobs/types';
import {
  ApiError,
  ConfirmationRequiredError,
  actionExecutionId,
  actionExecutionName,
  actionExecutionPrimaryOutput,
  actionExecutionRowIds,
  actionExecutionSheetId,
  isDeriveCompositeRequest,
  type ActionExecutionRequest,
  type ActionJob,
  type CopilotProposal,
  type RunActionLaunchResult,
  type RunEstimate,
  type RunProgress,
  type SheetMeta,
} from '../api/open';
import type { ProjectApiPort } from '../api/ports';
import { isActiveRunStatus, isTerminalActionJobStatus } from '../runStatusModel';
import { mergeDockJobs } from '../workbench/dockJobSummary';
import type { DockActionJob } from '../workbench/WorkbenchBottomDock';
import {
  columnBucket,
  durationBucket,
  failureCategory,
  rowBucket,
  sendProductTelemetry,
  telemetryActionKind,
  type AttemptKind,
  type FailureCategory,
} from '../telemetry/productTelemetry';

export interface CostGateState {
  estimate: RunEstimate;
  message: string;
  onConfirm(): void;
  /** Resolves promise-based confirmations (requestCostConfirmation) false. */
  onCancel?(): void;
}

/** A launch rejected by the server's overwrite-collision gate (precheck_fn,
 *  `output_column_exists` — mapped to HTTP 409, `_v1_action_result_http_status`).
 *  DISTINCT from the pre-submit CLIENT-side collision check ActionPanel
 *  already has (`newColumnConflictName`, keyed off the possibly-stale
 *  `sheet.columns` prop): this is what the form converts a SERVER 409 into,
 *  after the client's own stale-columns check missed a collision the server
 *  caught — e.g. a column created behind the form's back, in another tab or
 *  by a run this session's `sheet.columns` snapshot never observed. The SAME
 *  replacement-confirm affordance ActionPanel already renders for the
 *  client-detected case is what re-launches `onConfirm` (resubmit with
 *  `replace_existing: true`) — never automatic; the user must click it. */
export interface OutputColumnCollisionState {
  /** The colliding output column name(s), from the server's own
   *  ActionError.details.columns — server truth, not the client's
   *  (possibly stale) column list. */
  columns: string[];
  message: string;
  onConfirm(): void;
  onCancel(): void;
}

export interface ActionJobsState {
  error: string | null;
  jobs: DockActionJob[];
  loading: boolean;
}

interface QueuedJobTarget {
  jobId: number | null;
  receiptId: string | null;
}

export interface JobState {
  completedClusterReceiptId: string | null;
  run: RunProgress | null;
  actionJobs: ActionJobsState;
  costGate: CostGateState | null;
  outputColumnCollision: OutputColumnCollisionState | null;
}

/** Call-time collaborators the pre-migration hook received as its
 *  UseRunControllerArgs, minus `sheet`
 *  (passed directly to the one method that needs it, startRun) and `showError`
 *  is kept here since both orchestration AND the two poll ticks need it. */
export interface JobRunDeps {
  invalidateProjectData(): void;
  refreshHistory(): Promise<void> | void;
  refreshReviewCount(): Promise<void> | void;
  refreshSheets(): Promise<void> | void;
  showError(input: string | Error): void;
  /** Fired on the EARLIEST reliable
   *  success signal in the launch path — a run handle returned
   *  (handleRunStarted) or a queued action job accepted
   *  (handleQueuedActionJobStarted), i.e. "rows coming back". NOT fired on the
   *  synchronous-terminal branch (e.g. derive_join's new-sheet result, which
   *  keeps its drawer for tweak-and-re-run), nor on the confirm/collision/error
   *  paths (they throw, never reaching handleRunLaunchSettled). The action
   *  drawer subscribes to this to auto-close; preview never calls startRun. */
  onLaunchAccepted?(): void;
  /** Opens a sheet that a synchronous materializing action just created. */
  onMaterializedSheetCreated?(sheetId: string): void;
}

const initialActionJobsState: ActionJobsState = { error: null, jobs: [], loading: false };

function isTerminalReceiptStatus(status: string | null | undefined): boolean {
  return ['completed', 'partial', 'failed', 'cancelled'].includes(status ?? '');
}

function initialJobState(): JobState {
  return {
    completedClusterReceiptId: null,
    run: null,
    actionJobs: initialActionJobsState,
    costGate: null,
    outputColumnCollision: null,
  };
}

export interface JobStoreHandle {
  store: Store<JobState>;
  dismissCompletedClusterResult(): void;

  start(deps: JobRunDeps): void;
  refresh(): Promise<void>;
  liveActionJobs: {
    start(): void;
    dispose(): void;
  };
  startRun(req: ActionExecutionRequest, sheet: SheetMeta | null | undefined): void;
  startProposal(proposal: CopilotProposal, confirmed?: boolean): Promise<boolean>;
  cancelCurrentRun(runId: string): void;
  afterBackfill(runId: string): void;

  cancelCostGate(): void;
  confirmCostGate(): void;
  requestCostConfirmation(estimate: RunEstimate, message: string): Promise<boolean>;

  /** Mirrors cancelCostGate/
   *  confirmCostGate's pattern exactly (look up the current state, clear it,
   *  THEN invoke the closure — never invoke a stale one baked into rendered
   *  JSX). */
  cancelOutputColumnCollision(): void;
  confirmOutputColumnCollision(): void;

  dispose(): void;
}

export function createJobStore(
  projectId: string,
  projectApi: ProjectApiPort,
): JobStoreHandle {
  const store = createStore<JobState>(initialJobState());

  const runLane = createJobLane();
  const queuedLane = createJobLane();
  const dockLane = createJobLane();
  let runJob: Job<unknown> | null = null;
  let queuedJob: Job<unknown> | null = null;
  let dockJob: Job<unknown> | null = null;
  let runTarget: string | null = null;
  let queuedTarget: QueuedJobTarget | null = null;
  let runSlot: object | null = null;
  let queuedSlot: object | null = null;
  let dockSlot: object | null = null;
  let cancelSlot: { controller: AbortController } | null = null;
  let backfillSlot: { controller: AbortController } | null = null;
  let runInterval: ReturnType<typeof setInterval> | null = null;
  let queuedInterval: ReturnType<typeof setInterval> | null = null;
  let leadingTimer: ReturnType<typeof setTimeout> | null = null;
  let dockTimer: ReturnType<typeof setTimeout> | null = null;
  let visibilityListening = false;
  let active = false;
  let resourceGeneration = 0;
  let currentDeps: JobRunDeps | null = null;
  let baseJobs: DockActionJob[] = [];
  let dockJobs: DockActionJob[] | null = null;
  let dockInterested = false;
  let disposingDock = false;
  const baseRequestControllers = new Set<AbortController>();
  let disposingResource = false;
  let observedTelemetry: {
    action: string;
    attemptKind: AttemptKind;
    startedAt: number;
  } | null = null;

  function attemptKind(req: ActionExecutionRequest): AttemptKind {
    return req.confirmation || req.replace_existing ? 'retry' : 'initial';
  }

  function submittedTelemetry(
    req: ActionExecutionRequest,
    sheet: SheetMeta | null | undefined,
    startedAt: number,
    result: RunActionLaunchResult | null,
    error?: unknown,
  ): void {
    const status = result?.status;
    const accepted = Boolean(
      result?.runId
      || result?.jobId != null
      || result?.receiptId
      || status === 'queued'
      || status === 'running',
    );
    const outcome = error instanceof ConfirmationRequiredError
      ? 'needs_confirmation'
      : error
        ? error instanceof ApiError && error.status >= 400 && error.status < 500 ? 'rejected' : 'failed'
        : accepted ? 'queued' : status === 'cancelled' ? 'cancelled' : status === 'completed' ? 'success' : 'failed';
    const action = telemetryActionKind(actionExecutionId(req));
    const attempt = attemptKind(req);
    sendProductTelemetry({
      type: 'Action.runSubmitted',
      properties: {
        action,
        attemptKind: attempt,
        rows: rowBucket(actionExecutionRowIds(req)?.length ?? sheet?.rowCount),
        columns: columnBucket(undefined),
        requestDuration: durationBucket(performance.now() - startedAt),
        result: outcome,
        failureCategory: error ? failureCategory(error) : outcome === 'failed' ? 'unknown' : 'none',
      },
    }, projectId);
    if (accepted) observedTelemetry = { action, attemptKind: attempt, startedAt };
  }

  function observedResult(status: string | null | undefined): {
    result: 'success' | 'partial' | 'failed' | 'cancelled' | 'stalled' | 'orphaned' | 'no_live_worker';
    failure: FailureCategory;
  } {
    if (status === 'complete' || status === 'completed') return { result: 'success', failure: 'none' };
    if (status === 'partial') return { result: 'partial', failure: 'unknown' };
    if (status === 'cancelled') return { result: 'cancelled', failure: 'none' };
    if (status === 'stalled') return { result: 'stalled', failure: 'timeout' };
    if (status === 'orphaned') return { result: 'orphaned', failure: 'internal' };
    if (status === 'no_live_worker') return { result: 'no_live_worker', failure: 'missing_dependency' };
    return { result: 'failed', failure: 'unknown' };
  }

  function emitObserved(status: string | null | undefined): void {
    const telemetry = observedTelemetry;
    if (!telemetry) return;
    observedTelemetry = null;
    const outcome = observedResult(status);
    sendProductTelemetry({
      type: 'Action.runObserved',
      properties: {
        action: telemetry.action,
        attemptKind: telemetry.attemptKind,
        observedDuration: durationBucket(performance.now() - telemetry.startedAt),
        result: outcome.result,
        failureCategory: outcome.failure,
      },
    }, projectId);
  }

  // The launch lane is deliberately separate from the two polling lanes.
  // A launch can yield across catalog/spec/transport work before it assigns a
  // poll target, so supersession and workspace teardown must retire that
  // earlier async boundary too. There is no permanent disposed flag: the
  // WorkspaceStores lease may deactivate then reactivate the same store under
  // StrictMode, and the next explicit launch must start a fresh generation.
  interface ActionLaunch {
    readonly controller: AbortController;
    readonly resourceGeneration: number;
  }
  let currentActionLaunch: ActionLaunch | null = null;
  let disposingActionLaunch = false;
  let actionLaunchEpoch = 0;

  function beginActionLaunch(): ActionLaunch {
    if (disposingActionLaunch) {
      const retired = { controller: new AbortController(), resourceGeneration };
      retired.controller.abort();
      return retired;
    }
    const epoch = actionLaunchEpoch;
    const displaced = currentActionLaunch;
    // Invalidate the old identity before aborting it. Abort listeners run
    // synchronously and may re-enter a launch entrypoint; such a re-entrant
    // successor is newer than this outer attempt and must never be clobbered.
    currentActionLaunch = null;
    displaced?.controller.abort();
    const launch = { controller: new AbortController(), resourceGeneration };
    if (actionLaunchEpoch !== epoch || currentActionLaunch !== null) {
      launch.controller.abort();
      return launch;
    }
    actionLaunchEpoch += 1;
    currentActionLaunch = launch;
    return launch;
  }

  function isCurrentActionLaunch(launch: ActionLaunch): boolean {
    return active
      && launch.resourceGeneration === resourceGeneration
      && currentActionLaunch === launch
      && !launch.controller.signal.aborted;
  }

  /** Run one synchronous publication/collaborator call only for the live
   * launch, then report whether a re-entrant callback kept it live. */
  function performForCurrentActionLaunch(
    launch: ActionLaunch,
    effect: () => void,
  ): boolean {
    if (!isCurrentActionLaunch(launch)) return false;
    effect();
    return isCurrentActionLaunch(launch);
  }

  function retireActionLaunch(launch: ActionLaunch): void {
    if (currentActionLaunch === launch) currentActionLaunch = null;
  }

  function abortCurrentActionLaunch(): void {
    const launch = currentActionLaunch;
    if (!launch) return;
    currentActionLaunch = null;
    launch.controller.abort();
  }

  function setRun(next: RunProgress | null | ((prev: RunProgress | null) => RunProgress | null)): void {
    store.set((s) => {
      const run = typeof next === 'function' ? next(s.run) : next;
      return s.run === run ? s : { ...s, run };
    });
  }

  function isResourceCurrent(generation: number): boolean {
    return active && generation === resourceGeneration;
  }

  function documentIsHidden(): boolean {
    return typeof document !== 'undefined' && document.hidden === true;
  }

  function mergedJobs(): DockActionJob[] {
    return mergeDockJobs(baseJobs, dockJobs);
  }

  function actionJobsLoadStart(): void {
    store.set((s) => ({ ...s, actionJobs: { ...s.actionJobs, loading: true } }));
  }
  function actionJobsLoadSuccess(jobs: DockActionJob[]): void {
    baseJobs = jobs;
    store.set((s) => ({
      ...s,
      actionJobs: { error: null, jobs: mergedJobs(), loading: false },
    }));
  }
  function actionJobsLoadError(error: string): void {
    store.set((s) => ({ ...s, actionJobs: { ...s.actionJobs, error, loading: false } }));
  }

  function publishDockJobs(jobs: DockActionJob[]): void {
    dockJobs = jobs;
    store.set((s) => ({
      ...s,
      actionJobs: { ...s.actionJobs, jobs: mergedJobs() },
    }));
  }

  function actionJobIsActive(job: DockActionJob): boolean {
    const status = job.progress?.status;
    return status ? isActiveRunStatus(status) : !isTerminalActionJobStatus(job.status);
  }

  function setCostGate(
    next: CostGateState | null | ((prev: CostGateState | null) => CostGateState | null),
  ): void {
    store.set((s) => ({
      ...s,
      costGate: typeof next === 'function' ? next(s.costGate) : next,
    }));
  }

  function setOutputColumnCollision(
    next:
      | OutputColumnCollisionState
      | null
      | ((prev: OutputColumnCollisionState | null) => OutputColumnCollisionState | null),
  ): void {
    store.set((s) => ({
      ...s,
      outputColumnCollision:
        typeof next === 'function' ? next(s.outputColumnCollision) : next,
    }));
  }

  function takeCostGateCancel(gate: CostGateState | null): (() => void) | undefined {
    if (!gate) return undefined;
    const cancel = gate.onCancel;
    // Retire the callback before invoking it. Cancellation callbacks may
    // synchronously tear down the resource; disposal must not resolve the
    // same consent again while the first callback is still on-stack.
    gate.onCancel = undefined;
    return cancel;
  }

  function setRunTarget(runId: string | null): void {
    if (runId === null) {
      runJob = null;
      runTarget = null;
      if (runInterval !== null) {
        clearInterval(runInterval);
        runInterval = null;
      }
      // Invalidate the public-independent identity before abort listeners run;
      // a listener may synchronously install a fresh successor target.
      runLane.cancel();
      return;
    }
    const generation = resourceGeneration;
    const priorTarget = runTarget;
    const priorJob = runJob;
    const firstTarget = priorTarget === null;
    runSlot = null;
    const nextJob = runLane.start();
    if (
      !isResourceCurrent(generation)
      || runTarget !== priorTarget
      || runJob !== priorJob
    ) {
      if (runLane.isCurrent(nextJob)) runLane.cancel();
      return;
    }
    runTarget = runId;
    runJob = nextJob;
    if (active && firstTarget && runInterval === null) {
      runInterval = setInterval(() => void runPollTick(), 300);
    }
  }

  function setQueuedTarget(target: QueuedJobTarget | null): void {
    if (target === null) {
      queuedJob = null;
      queuedTarget = null;
      if (queuedInterval !== null) {
        clearInterval(queuedInterval);
        queuedInterval = null;
      }
      // As above, retire the identity/timer before synchronous abort re-entry.
      queuedLane.cancel();
      return;
    }
    const generation = resourceGeneration;
    const priorTarget = queuedTarget;
    const priorJob = queuedJob;
    const firstTarget = priorTarget === null;
    queuedSlot = null;
    const nextJob = queuedLane.start();
    if (
      !isResourceCurrent(generation)
      || queuedTarget !== priorTarget
      || queuedJob !== priorJob
    ) {
      if (queuedLane.isCurrent(nextJob)) queuedLane.cancel();
      return;
    }
    queuedTarget = target;
    queuedJob = nextJob;
    if (active && firstTarget) {
      if (queuedInterval === null) {
        queuedInterval = setInterval(() => void queuedPollTick(), 750);
      }
      if (!documentIsHidden()) void queuedPollTick();
    }
  }

  async function loadActionJobs(
    limit: number,
    generation: number,
    stillCurrent: () => boolean,
    signal: AbortSignal,
  ): Promise<DockActionJob[]> {
    const page = await projectApi.listActionJobs(null, limit, { projectId, signal });
    if (!isResourceCurrent(generation) || !stillCurrent()) return [];
    return page.jobs;
  }

  async function refresh(): Promise<void> {
    if (!active) return;
    const generation = resourceGeneration;
    const controller = new AbortController();
    baseRequestControllers.add(controller);
    actionJobsLoadStart();
    try {
      if (!isResourceCurrent(generation) || !baseRequestControllers.has(controller)) return;
      const jobs = await loadActionJobs(
        25,
        generation,
        () => baseRequestControllers.has(controller),
        controller.signal,
      );
      if (!isResourceCurrent(generation) || !baseRequestControllers.has(controller)) return;
      actionJobsLoadSuccess(jobs);
      if (
        dockInterested
        && dockTimer === null
        && dockSlot === null
        && !documentIsHidden()
        && jobs.some(actionJobIsActive)
      ) void dockPollTick();
    } catch (error) {
      if (!isResourceCurrent(generation) || !baseRequestControllers.has(controller)) return;
      actionJobsLoadError(error instanceof Error ? error.message : String(error));
    } finally {
      baseRequestControllers.delete(controller);
    }
  }

  function onVisibilityChange(): void {
    if (!active || documentIsHidden()) return;
    if (runTarget !== null) void runPollTick();
    if (queuedTarget !== null) void queuedPollTick();
    if (dockInterested) {
      if (dockTimer !== null) {
        clearTimeout(dockTimer);
        dockTimer = null;
      }
      void dockPollTick();
    }
  }

  function start(deps: JobRunDeps): void {
    if (disposingResource) return;
    currentDeps = deps;
    if (active) return;
    active = true;
    resourceGeneration += 1;
    if (typeof document !== 'undefined' && !visibilityListening) {
      document.addEventListener('visibilitychange', onVisibilityChange);
      visibilityListening = true;
    }
    const generation = resourceGeneration;
    leadingTimer = setTimeout(() => {
      leadingTimer = null;
      if (isResourceCurrent(generation)) void refresh();
    }, 0);
    if (dockInterested && !documentIsHidden()) void dockPollTick();
  }

  function startLiveActionJobs(): void {
    if (disposingResource || disposingDock) return;
    if (dockInterested) return;
    dockInterested = true;
    dockJob = dockLane.start();
    if (active && !documentIsHidden()) void dockPollTick();
  }

  function disposeLiveActionJobs(): void {
    if (!dockInterested && dockJobs === null) return;
    const wasDisposingDock = disposingDock;
    disposingDock = true;
    try {
      dockInterested = false;
      dockJob = null;
      dockSlot = null;
      if (dockTimer !== null) {
        clearTimeout(dockTimer);
        dockTimer = null;
      }
      dockLane.cancel();
      dockJobs = null;
      if (active) {
        store.set((s) => ({
          ...s,
          actionJobs: { ...s.actionJobs, jobs: mergedJobs() },
        }));
      }
    } finally {
      disposingDock = wasDisposingDock;
    }
  }

  function optimisticRunFor(
    req: ActionExecutionRequest,
    sheet: SheetMeta | null | undefined,
  ): RunProgress {
    const actionName = actionExecutionName(req);
    const rowIds = actionExecutionRowIds(req);
    const totalRows = rowIds?.length
      ? rowIds.length
      : (sheet?.rowCount ?? 0);
    return {
      runId: `pending-${Date.now()}`,
      actionName,
      actionKind: actionExecutionId(req),
      sheetId: actionExecutionSheetId(req),
      targetColumnId: actionExecutionPrimaryOutput(req),
      targetRowIds: rowIds ?? null,
      status: 'queued',
      completedRows: 0,
      totalRows,
      failedRows: 0,
      costSoFar: 0,
    };
  }

  function handleRunStarted(
    launch: ActionLaunch,
    runId: string,
    deps: Pick<JobRunDeps, 'refreshSheets' | 'onLaunchAccepted'>,
  ): void {
    if (!performForCurrentActionLaunch(
      launch,
      () => setRun((current) => (current ? { ...current, runId } : current)),
    )) return;
    // The run handle is back — rows are
    // coming. Close the launching drawer; the new-columns affordance takes
    // over the feedback role.
    if (!performForCurrentActionLaunch(launch, () => deps.onLaunchAccepted?.())) return;
    if (!performForCurrentActionLaunch(launch, () => { void deps.refreshSheets(); })) return;
    if (!performForCurrentActionLaunch(launch, () => { void refresh(); })) return;
    performForCurrentActionLaunch(launch, () => setRunTarget(runId));
  }

  function handleQueuedActionJobStarted(
    launch: ActionLaunch,
    target: QueuedJobTarget,
    deps: Pick<JobRunDeps, 'onLaunchAccepted'>,
  ): void {
    if (!performForCurrentActionLaunch(launch, () => setRun(null))) return;
    // A queued action job was accepted —
    // same earliest-success signal as a direct run handle.
    if (!performForCurrentActionLaunch(launch, () => deps.onLaunchAccepted?.())) return;
    if (!performForCurrentActionLaunch(launch, () => { void refresh(); })) return;
    performForCurrentActionLaunch(launch, () => setQueuedTarget(target));
  }

  function handleRunLaunchSettled(
    launch: ActionLaunch,
    result: RunActionLaunchResult,
    deps: JobRunDeps,
  ): void {
    if (!isCurrentActionLaunch(launch)) return;
    const { runId, jobId, receiptId, status, outputSheetId } = result;
    if (runId !== null) {
      handleRunStarted(launch, runId, deps);
      return;
    }
    if ((jobId != null || receiptId) && (status === 'queued' || status === 'running')) {
      handleQueuedActionJobStarted(
        launch,
        { jobId: jobId ?? null, receiptId: receiptId ?? null },
        deps,
      );
      return;
    }
    if (!performForCurrentActionLaunch(launch, () => setRun(null))) return;
    // Synchronous plain actions (cluster + the resolve.* transforms) come
    // back with no run/job handle — a completed status IS the success signal,
    // so close the launching drawer here too; the refreshes below surface the
    // new column as the feedback.
    if (
      status === 'completed'
      && !performForCurrentActionLaunch(launch, () => deps.onLaunchAccepted?.())
    ) return;
    if (!performForCurrentActionLaunch(launch, () => deps.invalidateProjectData())) return;
    let sheetRefresh: Promise<void> | void;
    if (!performForCurrentActionLaunch(launch, () => {
      sheetRefresh = deps.refreshSheets();
    })) return;
    if (status === 'completed' && outputSheetId) {
      const generation = launch.resourceGeneration;
      void Promise.resolve(sheetRefresh!).then(() => {
        if (!isResourceCurrent(generation)) return;
        deps.onMaterializedSheetCreated?.(outputSheetId);
      });
    }
    if (!performForCurrentActionLaunch(launch, () => { void deps.refreshHistory(); })) return;
    if (!performForCurrentActionLaunch(launch, () => { void deps.refreshReviewCount(); })) return;
    performForCurrentActionLaunch(launch, () => { void refresh(); });
  }

  function openRunCostGate(
    launch: ActionLaunch,
    req: ActionExecutionRequest,
    sheet: SheetMeta | null | undefined,
    deps: JobRunDeps,
    error: ConfirmationRequiredError,
  ): void {
    if (!performForCurrentActionLaunch(launch, () => setRun(null))) return;
    const consentedPromiseSetHash = error.estimate.promise_set_hash;
    const priorGate = store.get().costGate;
    const cancelPriorGate = takeCostGateCancel(priorGate);
    if (priorGate !== null && store.get().costGate === priorGate) setCostGate(null);
    cancelPriorGate?.();
    if (!isCurrentActionLaunch(launch) || store.get().costGate !== null) return;
    const gateEpoch = actionLaunchEpoch;
    setCostGate({
      estimate: error.estimate,
      message: error.message,
      onConfirm: () => {
        if (actionLaunchEpoch !== gateEpoch) return;
        if (!consentedPromiseSetHash) {
          deps.showError('The server did not provide the confirmation hash for this action.');
          return;
        }
        const confirmedRequest: ActionExecutionRequest = {
          ...req,
          confirmation: consentedPromiseSetHash,
        };
        startRunWithDeps(
          confirmedRequest,
          sheet,
          deps,
        );
      },
    });
  }

  function startRunWithDeps(
    req: ActionExecutionRequest,
    sheet: SheetMeta | null | undefined,
    deps: JobRunDeps,
  ): void {
    const launch = beginActionLaunch();
    const startedAt = performance.now();
    if (!performForCurrentActionLaunch(
      launch,
      () => setRun(optimisticRunFor(req, sheet)),
    )) return;
    projectApi
      .runAction(req, { projectId, signal: launch.controller.signal })
      .then((result) => {
        if (!isCurrentActionLaunch(launch)) return;
        submittedTelemetry(req, sheet, startedAt, result);
        handleRunLaunchSettled(launch, result, deps);
        const canonicalKind = isDeriveCompositeRequest(req) ? req.extraction.action_id : req.action_id;
        if (canonicalKind === 'cluster.values'
          && result.status === 'completed' && result.receiptId) {
          performForCurrentActionLaunch(launch, () => store.set((state) => ({
            ...state, completedClusterReceiptId: result.receiptId!,
          })));
        }
      })
      .catch((e: Error) => {
        if (!isCurrentActionLaunch(launch)) return;
        submittedTelemetry(req, sheet, startedAt, null, e);
        if (e instanceof ConfirmationRequiredError) {
          openRunCostGate(launch, req, sheet, deps, e);
        } else if (
          e instanceof ApiError
          && e.code === 'output_column_exists'
          && req.replace_existing === true
        ) {
          if (!performForCurrentActionLaunch(launch, () => setRun(null))) return;
          if (!performForCurrentActionLaunch(
            launch,
            () => setOutputColumnCollision(null),
          )) return;
          performForCurrentActionLaunch(launch, () => {
            deps.showError(
              new Error(
                `${e.message} — replacement was rejected. Choose another output name ` +
                  'or delete the protected column first.',
              ),
            );
          });
        } else if (
          e instanceof ApiError
          && e.code === 'output_column_exists'
        ) {
          // Server truth
          // wins over the client's possibly-stale column list — convert the
          // 409 into the SAME overwrite-confirm affordance instead of a
          // dead-end toast. Retrying re-submits the EXACT same request with
          // the request's one-run replacement flag. The server remains
          // authoritative about whether the target is replaceable.
          if (!performForCurrentActionLaunch(launch, () => setRun(null))) return;
          const columns = Array.isArray(e.details?.columns)
            ? (e.details!.columns as string[]).filter((c): c is string => typeof c === 'string')
            : [];
          const priorCollision = store.get().outputColumnCollision;
          if (priorCollision !== null) {
            if (!performForCurrentActionLaunch(launch, () => {
              if (store.get().outputColumnCollision === priorCollision) {
                setOutputColumnCollision(null);
              }
            })) return;
            if (!performForCurrentActionLaunch(launch, () => priorCollision.onCancel())) return;
          }
          if (store.get().outputColumnCollision !== null) return;
          const collisionEpoch = actionLaunchEpoch;
          setOutputColumnCollision({
            columns,
            message: e.message,
            onConfirm: () => {
              if (actionLaunchEpoch !== collisionEpoch) return;
              startRunWithDeps(
                { ...req, replace_existing: true },
                sheet,
                deps,
              );
            },
            onCancel: () => {},
          });
        } else {
          if (!performForCurrentActionLaunch(launch, () => setRun(null))) return;
          performForCurrentActionLaunch(launch, () => deps.showError(e));
        }
      })
      .finally(() => retireActionLaunch(launch));
  }

  function startRun(
    req: ActionExecutionRequest,
    sheet: SheetMeta | null | undefined,
  ): void {
    const deps = currentDeps;
    if (!active || !deps) return;
    startRunWithDeps(req, sheet, deps);
  }

  function startConfirmedProposal(
    proposal: CopilotProposal,
    deps: Pick<JobRunDeps, 'refreshSheets' | 'showError'>,
    consentedPromiseSetHash?: string,
  ): Promise<void> {
    const launch = beginActionLaunch();
    if (!isCurrentActionLaunch(launch)) return Promise.resolve();
    return projectApi.runProposal(
      proposal,
      true,
      consentedPromiseSetHash,
      { projectId, signal: launch.controller.signal },
    )
      .then(async (out) => {
        let refreshed: Promise<void> | void;
        if (!performForCurrentActionLaunch(launch, () => { refreshed = deps.refreshSheets(); })) return;
        if (out.run_id === null) await refreshed!;
        performForCurrentActionLaunch(launch, () => {
          if (out.run_id !== null) setRunTarget(String(out.run_id));
          else if (out.output_sheet_id) currentDeps?.onMaterializedSheetCreated?.(out.output_sheet_id);
        });
      })
      .catch((e: unknown) => {
        if (!isCurrentActionLaunch(launch)) return;
        if (e instanceof ConfirmationRequiredError) {
          openProposalCostGate(launch, proposal, deps, e);
          return;
        }
        performForCurrentActionLaunch(
          launch,
          () => deps.showError(e instanceof Error ? e.message : String(e)),
        );
      })
      .finally(() => retireActionLaunch(launch));
  }

  function openProposalCostGate(
    launch: ActionLaunch,
    proposal: CopilotProposal,
    deps: Pick<JobRunDeps, 'refreshSheets' | 'showError'>,
    error: ConfirmationRequiredError,
  ): void {
    if (!isCurrentActionLaunch(launch)) return;
    const priorGate = store.get().costGate;
    const cancelPriorGate = takeCostGateCancel(priorGate);
    if (priorGate !== null && store.get().costGate === priorGate) setCostGate(null);
    cancelPriorGate?.();
    if (!isCurrentActionLaunch(launch) || store.get().costGate !== null) return;
    const gateEpoch = actionLaunchEpoch;
    setCostGate({
      estimate: error.estimate,
      message: error.message,
      onConfirm: () => {
        if (actionLaunchEpoch !== gateEpoch) return;
        void startConfirmedProposal(
          proposal,
          deps,
          error.estimate.promise_set_hash,
        );
      },
    });
  }

  async function startProposalWithDeps(
    proposal: CopilotProposal,
    deps: Pick<JobRunDeps, 'refreshSheets' | 'showError'>,
    confirmed = false,
  ): Promise<boolean> {
    const launch = beginActionLaunch();
    if (!isCurrentActionLaunch(launch)) return false;
    try {
      const out = await projectApi.runProposal(
        proposal,
        confirmed,
        undefined,
        { projectId, signal: launch.controller.signal },
      );
      let refreshed: Promise<void> | void;
      if (!performForCurrentActionLaunch(launch, () => { refreshed = deps.refreshSheets(); })) {
        return false;
      }
      if (out.run_id === null) await refreshed!;
      if (!performForCurrentActionLaunch(launch, () => {
        if (out.run_id !== null) setRunTarget(String(out.run_id));
        else if (out.output_sheet_id) currentDeps?.onMaterializedSheetCreated?.(out.output_sheet_id);
      })) {
        return false;
      }
      return true;
    } catch (e: unknown) {
      if (!isCurrentActionLaunch(launch)) return false;
      if (e instanceof ConfirmationRequiredError) {
        openProposalCostGate(launch, proposal, deps, e);
        return false;
      }
      if (!performForCurrentActionLaunch(
        launch,
        () => deps.showError(e instanceof Error ? e.message : String(e)),
      )) return false;
      throw e;
    } finally {
      retireActionLaunch(launch);
    }
  }

  function startProposal(
    proposal: CopilotProposal,
    confirmed = false,
  ): Promise<boolean> {
    const deps = currentDeps;
    if (!active || !deps) return Promise.resolve(false);
    return startProposalWithDeps(proposal, deps, confirmed);
  }

  function cancelCurrentRun(runId: string): void {
    if (!active || !currentDeps) return;
    const generation = resourceGeneration;
    const displaced = cancelSlot;
    cancelSlot = null;
    displaced?.controller.abort();
    if (!isResourceCurrent(generation) || cancelSlot !== null) return;
    const slot = { controller: new AbortController() };
    cancelSlot = slot;
    const isCurrent = (): boolean => (
      isResourceCurrent(generation) && cancelSlot === slot
    );
    const perform = (effect: (deps: JobRunDeps) => void): boolean => {
      const deps = currentDeps;
      if (!isCurrent() || !deps) return false;
      effect(deps);
      return isCurrent();
    };
    void projectApi
      .cancelRun(runId, { projectId, signal: slot.controller.signal })
      .then((progress) => {
        if (!perform(() => setRun(progress))) return;
        if (!perform((deps) => deps.invalidateProjectData())) return;
        if (!perform((deps) => { void deps.refreshSheets(); })) return;
        if (!perform((deps) => { void deps.refreshHistory(); })) return;
        perform((deps) => { void deps.refreshReviewCount(); });
      })
      .catch((error: unknown) => {
        perform((deps) => deps.showError(error instanceof Error ? error.message : String(error)));
      })
      .finally(() => {
        if (cancelSlot === slot) cancelSlot = null;
      });
  }

  function afterBackfill(runId: string): void {
    if (!active || !currentDeps) return;
    const generation = resourceGeneration;
    const displaced = backfillSlot;
    backfillSlot = null;
    displaced?.controller.abort();
    if (!isResourceCurrent(generation) || backfillSlot !== null) return;
    const slot = { controller: new AbortController() };
    backfillSlot = slot;
    const isCurrent = (): boolean => (
      isResourceCurrent(generation) && backfillSlot === slot
    );
    void projectApi
      .getRunProgress(runId, { projectId, signal: slot.controller.signal })
      .then((progress) => {
        if (isCurrent()) setRun(progress);
      })
      .catch(() => undefined)
      .finally(() => {
        if (backfillSlot === slot) backfillSlot = null;
      });
    const deps = currentDeps;
    if (!isCurrent() || !deps) return;
    deps.invalidateProjectData();
    if (!isCurrent()) return;
    void deps.refreshSheets();
    if (!isCurrent()) return;
    void deps.refreshHistory();
    if (!isCurrent()) return;
    void deps.refreshReviewCount();
  }

  function cancelCostGate(): void {
    const current = store.get().costGate;
    const cancel = takeCostGateCancel(current);
    if (store.get().costGate === current) setCostGate(null);
    cancel?.();
  }

  function requestCostConfirmation(estimate: RunEstimate, message: string): Promise<boolean> {
    if (disposingResource) return Promise.resolve(false);
    const generation = resourceGeneration;
    return new Promise<boolean>((resolve) => {
      const current = store.get().costGate;
      const cancel = takeCostGateCancel(current);
      if (store.get().costGate === current) setCostGate(null);
      cancel?.();
      if (
        disposingResource
        || generation !== resourceGeneration
        || store.get().costGate !== null
      ) {
        resolve(false);
        return;
      }
      setCostGate({
        estimate,
        message,
        onConfirm: () => resolve(true),
        onCancel: () => resolve(false),
      });
    });
  }

  function confirmCostGate(): void {
    if (disposingResource) return;
    const current = store.get().costGate;
    if (current === null) return;
    const confirm = current?.onConfirm;
    const cancel = takeCostGateCancel(current);
    const epoch = actionLaunchEpoch;
    const launch = currentActionLaunch;
    const generation = resourceGeneration;
    if (store.get().costGate === current) setCostGate(null);
    if (
      disposingResource
      || resourceGeneration !== generation
      || actionLaunchEpoch !== epoch
      || currentActionLaunch !== launch
      || store.get().costGate !== null
    ) {
      cancel?.();
      return;
    }
    confirm?.();
  }

  function cancelOutputColumnCollision(): void {
    const current = store.get().outputColumnCollision;
    const cancel = current?.onCancel;
    if (store.get().outputColumnCollision === current) setOutputColumnCollision(null);
    cancel?.();
  }

  function confirmOutputColumnCollision(): void {
    if (disposingResource) return;
    const current = store.get().outputColumnCollision;
    if (current === null) return;
    const confirm = current?.onConfirm;
    const epoch = actionLaunchEpoch;
    const launch = currentActionLaunch;
    if (store.get().outputColumnCollision === current) setOutputColumnCollision(null);
    if (
      actionLaunchEpoch !== epoch
      || currentActionLaunch !== launch
      || store.get().outputColumnCollision !== null
    ) return;
    confirm?.();
  }

  async function runPollTick(): Promise<void> {
    if (!active || documentIsHidden() || runSlot !== null) return;
    const generation = resourceGeneration;
    const target = runTarget;
    const job = runJob;
    if (target === null || job === null) return;
    const slot = {};
    runSlot = slot;
    const isCurrent = (): boolean => (
      isResourceCurrent(generation)
      && runSlot === slot
      && runTarget === target
      && runLane.isCurrent(job)
    );
    try {
      const progress = await projectApi.getRunProgress(target, {
        projectId,
        signal: job.signal,
      });
      if (!isCurrent()) return;
      setRun(progress);
      if (!isCurrent()) return;
      if (!isActiveRunStatus(progress.status)) {
        emitObserved(progress.noLiveWorker ? 'no_live_worker' : progress.status);
        setRunTarget(null);
        const stillTerminal = (): boolean => (
          isResourceCurrent(generation) && runSlot === slot && runTarget === null
        );
        const deps = currentDeps;
        if (!stillTerminal() || !deps) return;
        deps.invalidateProjectData();
        if (!stillTerminal()) return;
        void deps.refreshSheets();
        if (!stillTerminal()) return;
        void deps.refreshHistory();
        if (!stillTerminal()) return;
        void deps.refreshReviewCount();
        if (!stillTerminal()) return;
        void refresh();
      }
    } catch {
      // Transient progress failures leave the fixed lane active for retry.
    } finally {
      if (runSlot === slot) runSlot = null;
    }
  }

  async function queuedPollTick(): Promise<void> {
    if (!active || documentIsHidden() || queuedSlot !== null) return;
    const generation = resourceGeneration;
    const target = queuedTarget;
    const epochJob = queuedJob;
    if (target === null || epochJob === null) return;
    const slot = {};
    queuedSlot = slot;
    const isCurrent = (): boolean => (
      isResourceCurrent(generation)
      && queuedSlot === slot
      && queuedTarget === target
      && queuedLane.isCurrent(epochJob)
    );
    const { jobId, receiptId } = target;
    try {
      let terminal = false;
      let terminalStatus: string | null = null;
      let job: ActionJob | null = null;
      if (jobId !== null) {
        if (projectApi.getActionJob) {
          job = await projectApi.getActionJob(jobId, {
            projectId,
            signal: epochJob.signal,
          });
        } else {
          const page = await projectApi.listActionJobs(null, 50, {
            projectId,
            signal: epochJob.signal,
          });
          job = page.jobs.find((item) => item.jobId === jobId) ?? null;
        }
        if (!isCurrent()) return;
        terminal = isTerminalActionJobStatus(job?.status);
        if (terminal) terminalStatus = job?.status ?? null;
      }
      if (!terminal && receiptId) {
        const receipt = await projectApi.getReceipt(receiptId, {
          projectId,
          signal: epochJob.signal,
        });
        if (!isCurrent()) return;
        terminal = isTerminalReceiptStatus(receipt.status);
        if (terminal) terminalStatus = receipt.status;
      }
      if (!isCurrent()) return;
      void refresh();
      if (!isCurrent()) return;
      if (terminal) {
        emitObserved(terminalStatus);
        setQueuedTarget(null);
        if (queuedSlot === slot) queuedSlot = null;
        const stillTerminal = (): boolean => (
          isResourceCurrent(generation) && queuedTarget === null && queuedSlot === null
        );
        const deps = currentDeps;
        if (!stillTerminal() || !deps) return;
        setRun(null);
        if (!stillTerminal()) return;
        deps.invalidateProjectData();
        if (!stillTerminal()) return;
        void deps.refreshSheets();
        if (!stillTerminal()) return;
        void deps.refreshHistory();
        if (!stillTerminal()) return;
        void deps.refreshReviewCount();
        if (!stillTerminal()) return;
        void refresh();
      }
      if (job?.error && isTerminalActionJobStatus(job.status)) {
        const deps = currentDeps;
        if (isResourceCurrent(generation) && queuedTarget === null && deps) {
          deps.showError(job.error);
        }
      }
    } catch {
      // Transient job/receipt failures leave the fixed lane active for retry.
    } finally {
      if (queuedSlot === slot) queuedSlot = null;
    }
  }

  async function dockPollTick(): Promise<void> {
    if (!active || !dockInterested || documentIsHidden() || dockSlot !== null) return;
    const generation = resourceGeneration;
    const job = dockJob;
    if (job === null) return;
    const slot = {};
    dockSlot = slot;
    const isCurrent = (): boolean => (
      isResourceCurrent(generation)
      && dockInterested
      && dockSlot === slot
      && dockLane.isCurrent(job)
    );
    let hasActiveJobs = false;
    try {
      const jobs = await loadActionJobs(
        25,
        generation,
        isCurrent,
        job.signal,
      );
      if (!isCurrent()) return;
      publishDockJobs(jobs);
      hasActiveJobs = jobs.some(actionJobIsActive);
    } catch {
      // Dock failures are silent; retain its last-known-good snapshot.
    } finally {
      if (dockSlot === slot) dockSlot = null;
      if (
        hasActiveJobs
        && isResourceCurrent(generation)
        && dockInterested
        && dockLane.isCurrent(job)
      ) {
        if (dockTimer !== null) clearTimeout(dockTimer);
        dockTimer = setTimeout(() => {
          dockTimer = null;
          void dockPollTick();
        }, 2_000);
      }
    }
  }

  function dispose(): void {
    const wasDisposingResource = disposingResource;
    disposingResource = true;
    // WEB-01 generation-first invariant: the whole resource is stale before
    // any synchronous abort listener can re-enter one of its entrypoints.
    active = false;
    resourceGeneration += 1;

    const wasDisposingActionLaunch = disposingActionLaunch;
    disposingActionLaunch = true;
    try {
      actionLaunchEpoch += 1;
      abortCurrentActionLaunch();

      if (leadingTimer !== null) clearTimeout(leadingTimer);
      if (dockTimer !== null) clearTimeout(dockTimer);
      if (runInterval !== null) clearInterval(runInterval);
      if (queuedInterval !== null) clearInterval(queuedInterval);
      leadingTimer = null;
      dockTimer = null;
      runInterval = null;
      queuedInterval = null;
      if (visibilityListening && typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibilityChange);
      }
      visibilityListening = false;

      for (const controller of baseRequestControllers) controller.abort();
      baseRequestControllers.clear();
      const cancel = cancelSlot;
      cancelSlot = null;
      cancel?.controller.abort();
      const backfill = backfillSlot;
      backfillSlot = null;
      backfill?.controller.abort();
      runLane.cancel();
      queuedLane.cancel();
      dockLane.cancel();
      runJob = null;
      queuedJob = null;
      dockJob = null;
      runTarget = null;
      queuedTarget = null;
      runSlot = null;
      queuedSlot = null;
      dockSlot = null;
      dockInterested = false;

      currentDeps = null;
      baseJobs = [];
      dockJobs = null;

      const pendingGate = store.get().costGate;
      const resolvePendingGate = takeCostGateCancel(pendingGate);
      resolvePendingGate?.();
      store.set((state) => ({
        ...state,
        completedClusterReceiptId: null,
        actionJobs: initialActionJobsState,
        costGate: null,
        outputColumnCollision: null,
      }));
    } finally {
      disposingActionLaunch = wasDisposingActionLaunch;
      disposingResource = wasDisposingResource;
    }
  }

  return {
    store,
    dismissCompletedClusterResult: () => store.set((state) => ({
      ...state, completedClusterReceiptId: null,
    })),
    start,
    refresh,
    liveActionJobs: {
      start: startLiveActionJobs,
      dispose: disposeLiveActionJobs,
    },
    startRun,
    startProposal,
    cancelCurrentRun,
    afterBackfill,
    cancelCostGate,
    confirmCostGate,
    requestCostConfirmation,
    cancelOutputColumnCollision,
    confirmOutputColumnCollision,
    dispose,
  };
}
