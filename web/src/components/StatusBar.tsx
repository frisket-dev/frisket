import { useEffect, useReducer, useRef, useState, type RefObject } from 'react';
import { Activity, AlertTriangle, ChevronUp, Inbox, Redo2, Square, Undo2, X } from 'lucide-react';
import { type HistoryState, type RunProgress, type RunRowsPage } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { formatUsd } from '../actions/model';
import { isActiveRunStatus, runCompletedWithErrors, runStatusLabel } from '../runStatusModel';
import { RunFailureTriage } from './RunFailureTriage';
import { useNativePopover } from '../hooks/useNativePopover';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import type { WorkbenchProjectionStatus } from '../workbench/WorkbenchBottomDock';

// A queued run whose worker never showed up must not spin forever: the
// reconciler flags no_live_worker so we can show a fix-it warning instead.
function isNoLiveWorker(run: RunProgress): boolean {
  return run.staleReason === 'no_live_worker' || Boolean(run.noLiveWorker);
}

function rowsProgress(run: RunProgress): string {
  return `${run.completedRows.toLocaleString()}/${run.totalRows.toLocaleString()} rows`;
}

function activeStatusCopy(run: RunProgress): string {
  if (isNoLiveWorker(run)) {
    return `no worker is running — queued work is stuck · ${rowsProgress(run)}`;
  }
  switch (run.status) {
    case 'queued':
      return `queued — waiting to start · ${rowsProgress(run)}`;
    case 'running':
      return `running · ${rowsProgress(run)}`;
    case 'stalled':
      return `stalled: waiting for worker progress · ${rowsProgress(run)}`;
    case 'orphaned':
      return `orphaned: waiting on run recovery · ${rowsProgress(run)}`;
    case 'failed':
      return `failed after ${rowsProgress(run)}`;
    case 'complete':
      return `${runStatusLabel(run.status, run.failedRows)} · ${run.totalRows.toLocaleString()} rows`;
    case 'cancelled':
      return `cancelled · ${run.totalRows.toLocaleString()} rows`;
    default:
      return `${run.status} · ${rowsProgress(run)}`;
  }
}

function pendingRunCopy(status: RunProgress['status']): string {
  switch (status) {
    case 'queued':
      return 'pending in worker queue';
    case 'stalled':
      return 'waiting for worker progress';
    case 'orphaned':
      return 'waiting on run recovery';
    case 'running':
      return 'filling pending AI cells';
    default:
      return 'run status updated';
  }
}

function compactStatusCopy(run: RunProgress): string {
  if (isActiveRunStatus(run.status)) return `${run.status} · ${rowsProgress(run)}`;
  if (run.status === 'failed') return `failed after ${rowsProgress(run)}`;
  // A run that finished with failed rows says so here rather than asserting
  // "complete" and contradicting itself two chips later with "· 3 failed".
  return `${runStatusLabel(run.status, run.failedRows)} · ${run.totalRows.toLocaleString()} rows`;
}

function actionTitle(name: string): string {
  return `Action: ${name}`;
}

function formatProjectionPointCount(count: number): string {
  return `${count.toLocaleString()} valid ${count === 1 ? 'point' : 'points'}`;
}

export interface StatusBarProps {
  run: RunProgress | null;
  history: HistoryState | null;
  reviewCount: number;
  projectionStatus: WorkbenchProjectionStatus | null;
  onUndo(): void;
  onRedo(): void;
  onOpenReview(): void;
  onCancelRun(runId: string): void;
  /** Failure triage (run watcher popover): filter the grid to the target
   *  column's failing rows for an outcome bucket ('any' = all buckets). */
  onShowFailedRows?(columnName: string, outcome: string): void;
  /** Failure triage: retry exactly one bucket's rows via run.backfill row_ids. */
  onRetryFailedRows?(columnName: string, outcome: string): void;
}

export function StatusBar({
  run, history, reviewCount, projectionStatus, onUndo, onRedo, onOpenReview, onCancelRun,
  onShowFailedRows, onRetryFailedRows,
}: StatusBarProps) {
  const canUndo = history !== null && history.undoTarget !== null;
  const canRedo = history !== null && history.redoTarget !== null;
  // The op the buttons would act on, surfaced as hover tooltips.
  const undoOp = canUndo ? history.cursorOp : null;
  const redoOp = canRedo
    ? history.ops.find((op) => op.index === history.redoTarget?.index) ?? null
    : null;

  const [watcherState, setWatcherState] = useState<{ runId: string | null; open: boolean }>({
    runId: null,
    open: false,
  });
  const runActive = run ? isActiveRunStatus(run.status) : false;
  const watcherOpen = Boolean(run && !runActive && watcherState.runId === run.runId && watcherState.open);
  const watcherTriggerRef = useRef<HTMLButtonElement>(null);
  const setCurrentWatcherOpen = (open: boolean | ((current: boolean) => boolean)) => {
    if (!run) return;
    setWatcherState((current) => {
      const currentOpen = current.runId === run.runId ? current.open : false;
      return {
        runId: run.runId,
        open: typeof open === 'function' ? open(currentOpen) : open,
      };
    });
  };

  return (
    <>
    {run && runActive && (
      <section
        className={`active-run-banner status-${run.status}`}
        data-testid="active-run-banner"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        <div className="active-run-main">
          {isNoLiveWorker(run) ? (
            <AlertTriangle size={16} className="run-warning-icon" aria-hidden data-testid="run-no-worker-warning" />
          ) : (
            <span className="run-spinner" aria-hidden />
          )}
          <div className="active-run-copy">
            <strong>{actionTitle(run.actionName)}</strong>
            <span>{activeStatusCopy(run)} · {formatUsd(run.costSoFar)}</span>
            {isNoLiveWorker(run) && (
              <span className="run-no-worker-hint">
                Start a worker (<code>frisket worker</code>) or check the worker container, then the queue drains.
              </span>
            )}
          </div>
        </div>
        <ActiveRunWatcher run={run} />
        <span className="pending-ai-cells" data-testid="pending-ai-cells">
          {pendingRunCopy(run.status)}
        </span>
      </section>
    )}
    <footer className="status-bar">
      <div className="status-left" data-testid="run-progress">
        {run ? (
          <>
            <button
              type="button"
              ref={watcherTriggerRef}
              className={`run-summary${watcherOpen ? ' run-summary-open' : ''}`}
              data-testid="run-watcher-toggle"
              data-run-status={run.status}
              data-run-completed-rows={run.completedRows}
              data-run-total-rows={run.totalRows}
              data-run-failed-rows={run.failedRows}
              title={runActive ? 'Run status shown above' : 'Run details'}
              aria-expanded={watcherOpen}
              onClick={() => {
                if (!runActive) setCurrentWatcherOpen((o) => !o);
              }}
            >
              {isActiveRunStatus(run.status) ? (
                <>
                  {isNoLiveWorker(run) ? (
                    <AlertTriangle size={12} className="run-warning-icon" aria-hidden />
                  ) : (
                    (run.status === 'running' || run.status === 'stalled' || run.status === 'orphaned') && (
                      <span className="run-spinner" aria-hidden />
                    )
                  )}
                  <span className="run-label">
                    {actionTitle(run.actionName)} · {compactStatusCopy(run)}
                    {run.failedRows > 0 && (
                      <span className="run-failed"> · {run.failedRows.toLocaleString()} failed</span>
                    )}{' '}
                    · {formatUsd(run.costSoFar)}
                  </span>
                  <span className="run-track">
                    <span
                      className="run-fill"
                      style={{ width: `${(100 * run.completedRows) / Math.max(1, run.totalRows)}%` }}
                    />
                  </span>
                </>
              ) : (
                <span
                  className={`run-label ${
                    // The "done" tone is a success signal — a run that left
                    // failed rows behind does not get it.
                    run.status === 'complete' && !runCompletedWithErrors(run.status, run.failedRows)
                      ? 'run-done'
                      : 'muted'
                  }`}
                >
                  {actionTitle(run.actionName)} · {compactStatusCopy(run)}
                  {run.failedRows > 0 && (
                    <span className="run-failed"> · {run.failedRows.toLocaleString()} failed</span>
                  )}{' '}
                  · {formatUsd(run.costSoFar)}
                </span>
              )}
              <ChevronUp size={12} className="run-chevron" aria-hidden />
            </button>
            {runActive && (
              <button
                type="button"
                className="mini-btn run-cancel"
                data-testid="run-cancel-button"
                onClick={() => onCancelRun(run.runId)}
              >
                <Square size={11} /> Cancel run
              </button>
            )}
            {watcherOpen && (
              <RunWatcher
                run={run}
                triggerRef={watcherTriggerRef}
                onClose={() => setCurrentWatcherOpen(false)}
                onShowFailedRows={onShowFailedRows}
                onRetryFailedRows={onRetryFailedRows}
              />
            )}
          </>
        ) : (
          <span className="run-label muted">idle</span>
        )}
        {projectionStatus && <ProjectionChip status={projectionStatus} />}
      </div>

      <div className="status-right">
        <button
          type="button"
          className="status-btn"
          data-testid="undo-button"
          onClick={onUndo}
          disabled={!canUndo}
          title={undoOp ? `Undo: ${undoOp.label}` : history?.undoTarget ? `Undo op ${history.undoTarget.id}` : 'Nothing to undo'}
        >
          <Undo2 size={13} /> Undo
        </button>
        <button
          type="button"
          className="status-btn"
          data-testid="redo-button"
          onClick={onRedo}
          disabled={!canRedo}
          title={redoOp ? `Redo: ${redoOp.label}` : history?.redoTarget ? `Redo op ${history.redoTarget.id}` : 'Nothing to redo'}
        >
          <Redo2 size={13} /> Redo
        </button>
        {reviewCount > 0 && (
          <>
            <span className="status-sep" />
            <button type="button" className="status-btn" data-testid="review-queue-button" onClick={onOpenReview} title="Review queue">
              <Inbox size={13} /> Review
              <span className="badge">{reviewCount.toLocaleString()}</span>
            </button>
          </>
        )}
      </div>
    </footer>
    </>
  );
}

/**
 * The active-projection readout. A projection is
 * always exactly one row, so it lives as a status-bar chip beside the run/idle
 * indicator rather than owning a bottom-dock tab; the click-through popover
 * carries the full backend/schema/generation detail.
 */
function ProjectionChip({ status }: { status: WorkbenchProjectionStatus }) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);

  return (
    <>
      <button
        type="button"
        ref={triggerRef}
        className={`run-summary projection-chip${open ? ' run-summary-open' : ''}`}
        data-testid="projection-chip"
        data-active-contribution-id={status.activeContributionId}
        data-projection-sheet-id={status.sheetId}
        data-projection-column-id={status.columnId}
        data-projection-backend={status.backend}
        data-projection-schema={status.schema}
        data-projection-transient={String(status.transient)}
        data-projection-valid-points={String(status.validPoints)}
        data-projection-generation={status.generation}
        aria-expanded={open}
        title="Projection details"
        onClick={() => setOpen((current) => !current)}
      >
        <Activity size={12} className="projection-chip-icon" aria-hidden />
        <span className="run-label">
          Map · {status.validPoints.toLocaleString()} {status.validPoints === 1 ? 'pt' : 'pts'}
          {status.transient && <span className="projection-chip-updating"> · updating</span>}
        </span>
        <ChevronUp size={12} className="run-chevron" aria-hidden />
      </button>
      {open && (
        <ProjectionWatcher
          status={status}
          triggerRef={triggerRef}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

/** Click-through detail popover for the projection chip; mirrors RunWatcher. */
function ProjectionWatcher({
  status,
  triggerRef,
  onClose,
}: {
  status: WorkbenchProjectionStatus;
  triggerRef: RefObject<HTMLButtonElement | null>;
  onClose(): void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useNativePopover(ref, onClose, {
    escape: false,
    ignoreSelector: '[data-testid="projection-chip"]',
  });
  const menuPos = useAnchoredPosition(triggerRef, {
    enabled: true,
    align: 'left',
    width: 300,
    gap: 4,
    minHeight: 100,
  });

  return (
    <div
      ref={ref}
      className="run-watcher projection-watcher"
      data-testid="projection-watcher"
      style={
        menuPos
          ? { position: 'fixed', inset: 'auto', top: menuPos.top, bottom: menuPos.bottom, left: menuPos.left, right: 'auto', width: menuPos.width, margin: 0 }
          : { position: 'fixed', visibility: 'hidden' }
      }
    >
      <div className="run-watcher-title">
        <div className="run-watcher-heading">
          Map projection
          <span
            className="run-watcher-status projection-status-freshness"
            data-testid="projection-status-freshness"
          >
            {status.transient ? 'Updating' : 'Ready'}
          </span>
        </div>
        <button
          type="button"
          className="icon-btn run-watcher-close"
          data-testid="projection-watcher-close"
          aria-label="Close projection details"
          title="Close"
          onClick={onClose}
        >
          <X size={14} aria-hidden />
        </button>
      </div>
      <div className="run-watcher-grid">
        <span className="prov-key">points</span>
        <span data-testid="projection-status-valid-points">
          {formatProjectionPointCount(status.validPoints)}
        </span>
        <span className="prov-key">column</span>
        <span>{status.columnName}</span>
        <span className="prov-key">backend</span>
        <span>{status.backend}</span>
        <span className="prov-key">schema</span>
        <span className="projection-watcher-mono">{status.schema}</span>
        <span className="prov-key">generation</span>
        <span className="projection-watcher-mono">{status.generation}</span>
      </div>
    </div>
  );
}

/**
 * Prominent active watcher surface: live row counts and cost, kept inline in
 * the banner so the footer stays a compact affordance during active runs.
 */
function ActiveRunWatcher({ run }: { run: RunProgress }) {
  return (
    <div className="run-watcher run-watcher-inline" data-testid="run-watcher">
      <RunWatcherBody run={run} />
    </div>
  );
}

/**
 * Per-run watcher popover for terminal details: row counts, failures, cost,
 * and the first few row-level error messages from the run-scoped inspector
 * endpoint.
 */
function RunWatcher({
  run,
  triggerRef,
  onClose,
  onShowFailedRows,
  onRetryFailedRows,
}: {
  run: RunProgress;
  triggerRef: RefObject<HTMLButtonElement | null>;
  onClose(): void;
  onShowFailedRows?: StatusBarProps['onShowFailedRows'];
  onRetryFailedRows?: StatusBarProps['onRetryFailedRows'];
}) {
  // Top-layer popover: a confirmed
  // no-Escape site by product decision (escape:false) — dismiss is outside-
  // click-only. `popover=manual` grants no UA light-dismiss so that contract
  // stays explicit rather than the UA silently adding Escape-dismiss. The
  // trigger toggles it, so its own click is ignored (not treated as outside).
  const ref = useRef<HTMLDivElement>(null);
  useNativePopover(ref, onClose, {
    escape: false,
    ignoreSelector: '[data-testid="run-watcher-toggle"]',
  });
  // Fixed-position anchor now that the popover is a top-layer element — it no
  // longer inherits placement from `.status-left`'s CSS positioned ancestor
  // (styles.css `.run-watcher`, bottom:32px;left:0 above the trigger). Space
  // below a status-bar trigger is always near-zero, so the shared flip
  // predicate naturally resolves upward, matching the original fixed offset.
  const menuPos = useAnchoredPosition(triggerRef, {
    enabled: true,
    align: 'left',
    width: 380,
    gap: 4,
    minHeight: 100,
  });

  return (
    <div
      ref={ref}
      className="run-watcher run-watcher-popover"
      data-testid="run-watcher"
      style={
        menuPos
          ? { position: 'fixed', inset: 'auto', top: menuPos.top, bottom: menuPos.bottom, left: menuPos.left, right: 'auto', width: menuPos.width, margin: 0 }
          : { position: 'fixed', visibility: 'hidden' }
      }
    >
      <RunWatcherBody
        run={run}
        onClose={onClose}
        onShowFailedRows={onShowFailedRows}
        onRetryFailedRows={onRetryFailedRows}
      />
    </div>
  );
}

function RunWatcherBody({
  run,
  onClose,
  onShowFailedRows,
  onRetryFailedRows,
}: {
  run: RunProgress;
  onClose?: () => void;
  onShowFailedRows?: StatusBarProps['onShowFailedRows'];
  onRetryFailedRows?: StatusBarProps['onRetryFailedRows'];
}) {
  const pct = (100 * run.completedRows) / Math.max(1, run.totalRows);
  // Triage acts on the run's target column; a run restored without launch
  // context has no column name — the buckets still render, the actions do not.
  const triageColumn = run.targetColumnId;

  return (
    <>
      <div className="run-watcher-title">
        <div className="run-watcher-heading">
          {actionTitle(run.actionName)}
          <span className={`run-watcher-status ${runCompletedWithErrors(run.status, run.failedRows)
            ? 'run-failed' : `status-${run.status}`}`}>
            {runStatusLabel(run.status, run.failedRows)}
          </span>
        </div>
        {onClose && (
          <button
            type="button"
            className="icon-btn run-watcher-close"
            data-testid="run-watcher-close"
            aria-label="Close run details"
            title="Close"
            onClick={onClose}
          >
            <X size={14} aria-hidden />
          </button>
        )}
      </div>
      <div className="run-watcher-grid">
        <span className="prov-key">rows</span>
        <span>
          {run.completedRows.toLocaleString()} / {run.totalRows.toLocaleString()} ({pct.toFixed(0)}%)
        </span>
        <span className="prov-key">failed</span>
        <span className={run.failedRows > 0 ? 'run-failed' : undefined}>
          {run.failedRows.toLocaleString()}
        </span>
        <span className="prov-key">cost</span>
        <span>{formatUsd(run.costSoFar)}</span>
      </div>
      {!isActiveRunStatus(run.status) && run.rowErrors && (
        <RunFailureTriage
          rowErrors={run.rowErrors}
          onShowRows={
            triageColumn && onShowFailedRows
              ? (outcome) => {
                  onShowFailedRows(triageColumn, outcome);
                  // Close the popover so the freshly filtered grid is visible.
                  onClose?.();
                }
              : undefined
          }
          onRetryRows={
            triageColumn && onRetryFailedRows
              ? (outcome) => onRetryFailedRows(triageColumn, outcome)
              : undefined
          }
        />
      )}
      {run.failedRows > 0 && (
        <RunWatcherErrors key={`${run.runId}:${run.failedRows}`} runId={run.runId} />
      )}
    </>
  );
}

type RunRowsAction =
  | { type: 'loaded'; page: RunRowsPage }
  | { type: 'failed' };

function runRowsReducer(_state: RunRowsPage | null, action: RunRowsAction): RunRowsPage | null {
  return action.type === 'loaded' ? action.page : null;
}

function RunWatcherErrors({ runId }: { runId: string }) {
  const { projectApi: api } = useWorkspaceStores();
  const [rowsPage, dispatch] = useReducer(runRowsReducer, null);

  useEffect(() => {
    let alive = true;
    api
      .getRunRows(runId, 0, 20, 'error')
      .then((page) => {
        if (alive) dispatch({ type: 'loaded', page });
      })
      .catch(() => {
        if (alive) dispatch({ type: 'failed' });
      });
    return () => { alive = false; };
  }, [runId]);

  const failedRows = (rowsPage?.rows ?? []).filter((row) => row.status === 'error');
  const shown = failedRows.slice(0, 5);

  return (
    <div className="run-watcher-errors" data-testid="run-inspector-rows">
      <div className="run-watcher-errors-title">
        Failed rows
        {(rowsPage?.total ?? failedRows.length) > shown.length
          ? ` (first ${shown.length})`
          : ''}
      </div>
      {rowsPage === null ? (
        <div className="muted">loading…</div>
      ) : shown.length === 0 ? (
        <div className="muted">no failed row details recorded</div>
      ) : (
        <ul>
          {shown.map((row) => {
            const retry = row.retries[0];
            return (
              <li key={row.rowId} data-testid="run-inspector-row">
                <span className="run-err-loc">row {row.rowIndex + 1}</span>
                <span className="run-err-msg">{row.error}</span>
                {row.retryCount > 0 && (
                  <span className="run-retry">
                    {row.retryCount} retry{row.retryCount === 1 ? '' : 'ies'}
                    {retry?.error ? ` · ${String(retry.error)}` : ''}
                    {retry?.status ? ` (${String(retry.status)})` : ''}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
