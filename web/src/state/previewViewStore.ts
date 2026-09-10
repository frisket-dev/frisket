// Owns the in-memory action preview slice plus the poll/generation/teardown
// lifecycle that drives it. workspace/useWorkspaceModel.tsx is the bind-side
// consumer: it supplies call-time collaborators and preserves the existing
// unmount/sheet-change/re-entry teardown triggers. This file is React-free and
// app-free, matching state/jobStore.ts's split from bind/useRunController.ts.
//
// createPreviewViewStore() is per-project, assembled by
// state/createWorkspaceStores.ts — never a module-level singleton.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';
import {
  ConfirmationRequiredError,
  actionExecutionName,
  actionExecutionSheetId,
  isRegisteredActionRequest,
  type ActionExecutionRequest,
  type RunEstimate,
  type SheetMeta,
  type PreviewSampleResult,
  type PreviewStatus,
} from '../api/open';
import type { ProjectApiPort } from '../api/ports';

// Cadence for polling an in-memory action preview job (start -> poll -> done).
const PREVIEW_POLL_MS = 750;

/** Call-time collaborators from useWorkspaceModel. The store is constructed
 * before either callback exists, so neither may be captured at construction. */
export interface PreviewViewDeps {
  invalidateProjectData(): void;
  requestCostConfirmation(estimate: RunEstimate, message: string): Promise<boolean>;
  onComplete?(result: PreviewSampleResult): void;
}

/** An in-memory action preview: a small sample computed server-side and held
 *  HERE in store memory only (never persisted, cleared on sheet switch). While
 *  `running` the banner shows progress; once `done` the grid pages EXACTLY the
 *  sampled row ids (the lensRowIds seam) and overlays the sampled columns/values
 *  — no op/run/column/cell is ever written. Closing drops this object; "Run for
 *  real" fires the normal run from `req`. null = no preview open. */
export interface PreviewGridView {
  accounting?: PreviewSampleResult['accounting'];
  /** Launch context only; source-free actions have no sheet identity. */
  sheetId: string | null;
  /** The server job id, for polling and cancellation. */
  previewId: string;
  status: PreviewStatus;
  progress: { done: number; total: number | null };
  result: PreviewSampleResult | null;
  actionName: string;
  rowCount: number;
  totalRows: number | null;
  /** Present when `status === 'error'`: the failure message for the banner. */
  error: string | null;
  /** The originating request, replayed (without previewRows) by "Run for real". */
  req: ActionExecutionRequest;
}

export interface PreviewViewState {
  /** Active preview run rendered as a scoped sheet view. null = no preview. */
  previewView: PreviewGridView | null;
}

export function createPreviewViewState(): PreviewViewState {
  return { previewView: null };
}

export function previewViewForSheet(
  view: PreviewGridView | null,
  sheetId: string | null | undefined,
): PreviewGridView | null {
  return view && (view.sheetId === null || view.sheetId === sheetId) ? view : null;
}

export function createPreviewViewStore(api: Pick<
  ProjectApiPort,
  'startPreview' | 'getPreview' | 'cancelPreview'
>): {
  store: Store<PreviewViewState>;
  /** = closePreviewView / runPreviewForReal: an unconditional clear (always
   *  notifies, even when previewView is already null). */
  clearPreviewView(): void;

  /** Starts the server preview and owns its settle-relative recursive poll. */
  openPreviewView(
    req: ActionExecutionRequest,
    sheet: Pick<SheetMeta, 'id' | 'rowCount'> | null,
    deps: PreviewViewDeps,
  ): Promise<void>;
  /** Bumps the shared generation, clears the poll timer, and cancels the
   *  current server preview fire-and-forget. */
  teardownPreviewJob(): void;
  /** Project-close participant: the same teardown path used by the hook's
   *  unmount, sheet-change, and self-superseding launch triggers. */
  dispose(): void;

  /** = the 'selectSheet' reducer case's previewView-owned half (unconditional):
   *  composed alongside lensView/lensOpenError/workView at
   *  state/workspaceTransitions.ts's resetForRouteSheetChange, the fixed
   *  class-1/2 sheet-change reset. */
  resetForSheetChange(): void;
} {
  const store = createStore<PreviewViewState>(createPreviewViewState());

  // Store-side equivalent of the former hook-local previewJobRef. The timer is
  // settle-relative: every retry is armed only after the preceding request
  // settles, preserving the original recursive setTimeout semantics.
  const previewJob: {
    previewId: string | null;
    timer: ReturnType<typeof setTimeout> | null;
    onVisibility: (() => void) | null;
  } = { previewId: null, timer: null, onVisibility: null };
  // Store-side equivalent of previewGenRef. Every teardown trigger reaches
  // teardownPreviewJob below, so one counter guards every async branch.
  let previewGen = 0;

  function setPreviewView(view: PreviewGridView): void {
    store.set((s) => ({ ...s, previewView: view }));
  }

  function updatePreviewViewIf(
    identity: 'sheetId' | 'previewId',
    expected: string | null,
    patch: Partial<PreviewGridView> | null,
  ): void {
    store.set((s) => {
      const current = s.previewView;
      return {
        ...s,
        previewView:
          current?.[identity] === expected
            ? patch === null
              ? null
              : { ...current, ...patch }
            : current,
      };
    });
  }

  function clearPreviewView(): void {
    store.set((s) => ({ ...s, previewView: null }));
  }

  function teardownPreviewJob(): void {
    previewGen += 1;
    const job = previewJob;
    if (job.timer != null) {
      clearTimeout(job.timer);
      job.timer = null;
    }
    if (job.onVisibility != null) {
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', job.onVisibility);
      }
      job.onVisibility = null;
    }
    if (job.previewId) {
      // DELETE is idempotent server-side; a failed cancel (already-gone job) is
      // a no-op. Fire-and-forget — teardown must not await.
      void api.cancelPreview(job.previewId).catch(() => {});
      job.previewId = null;
    }
  }

  async function openPreviewView(
    req: ActionExecutionRequest,
    sheet: Pick<SheetMeta, 'id' | 'rowCount'> | null,
    deps: PreviewViewDeps,
  ): Promise<void> {
    // Replace any in-flight preview (cancels its server job + stops its timer,
    // and bumps the generation so this call owns the previewView).
    teardownPreviewJob();
    const gen = previewGen;
    const sheetId = actionExecutionSheetId(req) || null;
    const actionName = actionExecutionName(req);
    const totalRows = sheetId === null ? null : sheet?.rowCount ?? null;
    // Show the running banner immediately (no server id yet).
    setPreviewView({
      sheetId,
      previewId: '',
      status: 'running',
      progress: { done: 0, total: null },
      result: null,
      actionName,
      rowCount: 0,
      totalRows,
      error: null,
      req,
    });

    let started: Awaited<ReturnType<typeof api.startPreview>>;
    let launchReq = req;
    while (true) {
      try {
        started = await api.startPreview(launchReq);
        break;
      } catch (e) {
        if (previewGen !== gen) return;
        if (e instanceof ConfirmationRequiredError) {
          const confirmed = await deps.requestCostConfirmation(e.estimate, e.message);
          if (previewGen !== gen) return;
          if (!confirmed) {
            clearPreviewView();
            return;
          }
          if (isRegisteredActionRequest(launchReq) && !e.estimate.promise_set_hash) {
            updatePreviewViewIf('sheetId', sheetId, {
              status: 'error',
              error: 'The server did not provide the confirmation hash for this action.',
            });
            return;
          }
          launchReq = { ...launchReq, confirmation: e.estimate.promise_set_hash };
          continue;
        }
        const message = e instanceof Error ? e.message : String(e);
        updatePreviewViewIf('sheetId', sheetId, { status: 'error', error: message });
        return;
      }
    }
    // A teardown landed while the start was in flight: cancel this orphan job
    // (teardown couldn't — it didn't have the id yet) and bail.
    if (previewGen !== gen) {
      void api.cancelPreview(started.previewId).catch(() => {});
      return;
    }
    const previewId = started.previewId;
    previewJob.previewId = previewId;
    updatePreviewViewIf('sheetId', sheetId, {
      previewId,
      progress: { done: 0, total: started.total },
      rowCount: started.total ?? 0,
    });

    // A single transient poll failure (offline blip, server restart) must not
    // flip a still-running job to a terminal error banner — retry a couple of
    // times before giving up. Successful polls reset the budget.
    let pollFailures = 0;
    let pollInFlight = false;
    const poll = async () => {
      if (previewGen !== gen) return;
      previewJob.timer = null;
      if (typeof document !== 'undefined' && document.hidden === true) {
        return;
      }
      if (pollInFlight) return;
      pollInFlight = true;
      let res;
      try {
        try {
          res = await api.getPreview(previewId);
        } finally {
          pollInFlight = false;
        }
      } catch (e) {
        if (previewGen !== gen) return;
        pollFailures += 1;
        if (pollFailures < 3) {
          previewJob.timer = setTimeout(() => void poll(), PREVIEW_POLL_MS);
          return;
        }
        previewJob.previewId = null;
        previewJob.timer = null;
        const message = e instanceof Error ? e.message : String(e);
        updatePreviewViewIf('previewId', previewId, { status: 'error', error: message });
        return;
      }
      pollFailures = 0;
      if (previewGen !== gen) return;
      updatePreviewViewIf('previewId', previewId, { accounting: res.accounting ?? null });
      if (res.status === 'running') {
        updatePreviewViewIf('previewId', previewId, { status: 'running', progress: res.progress });
        previewJob.timer = setTimeout(() => void poll(), PREVIEW_POLL_MS);
        return;
      }
      // Terminal states — the server job is gone, so there is nothing to cancel.
      previewJob.previewId = null;
      previewJob.timer = null;
      if (res.status === 'done') {
        updatePreviewViewIf('previewId', previewId, {
          status: 'done',
          progress: res.progress,
          result: res,
          rowCount: res.sampled,
          totalRows: res.total,
          error: null,
          req,
        });
        deps.onComplete?.(res);
        deps.invalidateProjectData();
      } else if (res.status === 'cancelled') {
        updatePreviewViewIf('previewId', previewId,
          res.accounting ? { status: 'cancelled', progress: res.progress } : null);
      } else {
        const message = res.error?.message ?? 'Preview failed.';
        updatePreviewViewIf('previewId', previewId, { status: 'error', error: message });
      }
    };
    const onVisibility = () => {
      if (
        previewJob.previewId !== previewId ||
        pollInFlight ||
        (typeof document !== 'undefined' && document.hidden === true)
      ) return;
      if (previewJob.timer != null) {
        clearTimeout(previewJob.timer);
        previewJob.timer = null;
      }
      void poll();
    };
    previewJob.onVisibility = onVisibility;
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility);
    }
    previewJob.timer = setTimeout(() => void poll(), PREVIEW_POLL_MS);
  }

  return {
    store,
    clearPreviewView,
    openPreviewView,
    teardownPreviewJob,
    dispose: teardownPreviewJob,
    resetForSheetChange: clearPreviewView,
  };
}

export type PreviewViewStoreHandle = ReturnType<typeof createPreviewViewStore>;
