import { useEffect, useReducer, useRef, useState, type RefObject } from 'react';
import { Activity, AlertTriangle, ChevronUp, Inbox, Redo2, Square, Undo2, X } from 'lucide-react';
import { type HistoryState, type RunProgress, type RunRowsPage } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { formatUsd } from '../actions/model';
import { isActiveRunStatus, runCompletedWithErrors, runStatusLabel } from '../runStatusModel';
import { RunFailureTriage } from './RunFailureTriage';
import { SearchPreparationNotice } from './SearchPreparationNotice';
import {
  composeImportSearchActivity,
  preferredImportActivity,
  type ImportProgressController,
} from './useImportProgress';
import { useNativePopover } from '../hooks/useNativePopover';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import type { WorkbenchProjectionStatus } from '../workbench/WorkbenchBottomDock';
import { useEditionModule } from '../editions/module';
import {
  useSearchPreparationNotice,
  type SearchPreparationNotice as SearchPreparationNoticeModel,
} from './useSearchPreparationNotice';
import { ActivityProgressCard, ProgressRow } from './ActivityProgress';

// A queued run whose worker never showed up must not spin forever: the
// reconciler flags no_live_worker so we can show a fix-it warning instead.
function isNoLiveWorker(run: RunProgress): boolean {
  return run.staleReason === 'no_live_worker' || Boolean(run.noLiveWorker);
}

function rowsProgress(run: RunProgress): string {
  const unit = run.actionKind === 'media.ocr' ? 'files' : 'rows';
  return `${run.completedRows.toLocaleString()}/${run.totalRows.toLocaleString()} ${unit}`;
}

function activeOcrFiles(run: RunProgress) {
  return isActiveRunStatus(run.status) ? run.workProgress?.files ?? [] : [];
}

function activeStatusCopy(run: RunProgress): string {
  if (run.cancelRequested && isActiveRunStatus(run.status)) return 'Stopping…';
  if (isNoLiveWorker(run)) {
    return 'no worker is running — queued work is stuck';
  }
  switch (run.status) {
    case 'queued':
      return 'queued — waiting to start';
    case 'running':
      return 'running';
    case 'stalled':
      return 'stalled: waiting for worker progress';
    case 'orphaned':
      return 'orphaned: waiting on run recovery';
    default:
      return run.status;
  }
}

function compactStatusCopy(run: RunProgress): string {
  if (run.cancelRequested && isActiveRunStatus(run.status)) return `Stopping… · ${rowsProgress(run)}`;
  if (isActiveRunStatus(run.status)) return `${run.status} · ${rowsProgress(run)}`;
  if (run.status === 'failed') return `failed after ${rowsProgress(run)}`;
  // A run that finished with failed rows says so here rather than asserting
  // "complete" and contradicting itself two chips later with "· 3 failed".
  return `${runStatusLabel(run.status, run.failedRows)} · ${run.totalRows.toLocaleString()} rows`;
}

function actionTitle(name: string): string {
  return `Action: ${name}`;
}

export function ActionProgressNotice({
  run,
  onCancel,
  onDismiss,
}: {
  run: RunProgress;
  onCancel(runId: string): void;
  onDismiss(): void;
}) {
  const noLiveWorker = isNoLiveWorker(run);
  return (
    <ActivityProgressCard
      state={noLiveWorker ? 'warning' : 'active'}
      title={actionTitle(run.actionName)}
      detail={(
        <>
          <span>{activeStatusCopy(run)} · {formatUsd(run.costSoFar)}</span>
          {noLiveWorker && (
            <span className="run-no-worker-hint">
              Start a worker (<code>frisket worker</code>) or check the worker container, then the queue drains.
            </span>
          )}
        </>
      )}
      indicator={noLiveWorker ? (
        <AlertTriangle size={16} className="run-warning-icon" aria-hidden data-testid="run-no-worker-warning" />
      ) : undefined}
      onDismiss={onDismiss}
      dismissLabel="Minimize action progress"
      dismissTestId="active-run-dismiss"
      testId="active-run-banner"
      className={`active-run-banner run-status-${run.status}`}
      footer={(
        <button
          type="button"
          className="mini-btn run-cancel"
          data-testid="active-run-cancel"
          disabled={run.cancelRequested}
          onClick={() => onCancel(run.runId)}
        >
          <Square size={11} /> {run.cancelRequested ? 'Stopping…' : 'Cancel run'}
        </button>
      )}
    >
      <ActiveRunWatcher run={run} />
    </ActivityProgressCard>
  );
}

function formatProjectionPointCount(count: number): string {
  return `${count.toLocaleString()} valid ${count === 1 ? 'point' : 'points'}`;
}

const IDLE_IMPORT_PROGRESS: ImportProgressController = {
  activities: [],
  message: null,
  busyRef: null,
  preparingRef: null,
  resume() {},
  async stop() {},
  keep() {},
};

export interface StatusBarProps {
  run: RunProgress | null;
  history: HistoryState | null;
  reviewCount: number;
  projectionStatus: WorkbenchProjectionStatus | null;
  importProgress?: ImportProgressController;
  importPriority?: { sheetId: string; rowId: string; label: string } | null;
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
  run, history, reviewCount, projectionStatus,
  importProgress = IDLE_IMPORT_PROGRESS, importPriority = null,
  onUndo, onRedo, onOpenReview, onCancelRun,
  onShowFailedRows, onRetryFailedRows,
}: StatusBarProps) {
  const { actSurface, chrome, chromePreferences: { projectId } } = useWorkspaceStores();
  const { descriptor } = useEditionModule();
  const baseSearchPreparationNotice = useSearchPreparationNotice(
    projectId,
    descriptor.capabilities.team,
  );
  const importActivity = preferredImportActivity(importProgress.activities);
  const combinedActivity = composeImportSearchActivity(importActivity, baseSearchPreparationNotice);
  const searchPreparationNotice = combinedActivity?.notice ?? baseSearchPreparationNotice;
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
  const [runNoticeVisibility, setRunNoticeVisibility] = useState<{
    projectId: string; runId: string | null; open: boolean;
  }>({ projectId, runId: null, open: false });
  const [searchNoticeVisibility, setSearchNoticeVisibility] = useState<{
    projectId: string; activityKey: string | null; open: boolean;
  }>({ projectId, activityKey: null, open: false });
  // Each new action shows its own progress. Background search only opens
  // through its own control; neither activity can reopen the other.
  const runNoticeOpen = Boolean(runActive && run && (
    runNoticeVisibility.projectId !== projectId
    || runNoticeVisibility.runId !== run.runId
    || runNoticeVisibility.open
  ));
  const searchNoticeOpen = Boolean(searchPreparationNotice
    && searchNoticeVisibility.projectId === projectId
    && searchNoticeVisibility.activityKey === searchPreparationNotice.activityKey
    && searchNoticeVisibility.open);
  const setRunNoticeOpen = (open: boolean) => {
    setRunNoticeVisibility({ projectId, runId: run?.runId ?? null, open });
  };
  const setSearchNoticeOpen = (open: boolean) => {
    setSearchNoticeVisibility({
      projectId,
      activityKey: searchPreparationNotice?.activityKey ?? null,
      open,
    });
  };
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
    {(runNoticeOpen || searchNoticeOpen) && (
      <div className="status-notice-stack" data-testid="status-notice-stack">
        {run && runNoticeOpen && (
          <ActionProgressNotice
            run={run}
            onCancel={onCancelRun}
            onDismiss={() => setRunNoticeOpen(false)}
          />
        )}
        {searchPreparationNotice && searchNoticeOpen && (
          <SearchPreparationNotice
            notice={searchPreparationNotice}
            onReviewPreparation={actSurface.openImportPreparation}
            onStopPreparation={importActivity?.canStop
              ? () => importProgress.stop(importActivity)
              : importActivity ? undefined : searchPreparationNotice.onStopPreparation}
            progressLines={combinedActivity?.lines}
            now={importActivity && importPriority?.sheetId === String(importActivity.session.sheet_id)
              ? importPriority.label
              : null}
            stopLabel={importActivity?.session.intake_kind === 'table' ? 'Stop import' : importActivity ? 'Stop remaining' : undefined}
            onResumeImport={importActivity?.needsRecovery && importActivity.session.state !== 'cancelled'
              ? () => importProgress.resume(importActivity)
              : undefined}
            onKeepImport={importActivity?.needsRecovery
              ? () => importProgress.keep(importActivity)
              : undefined}
            importActionBusy={importProgress.busyRef === importActivity?.importRef
              || importProgress.preparingRef === importActivity?.importRef}
            importMessage={importActivity ? importProgress.message : null}
            importProblemCount={importActivity?.session.problem_count}
            importSourceLabel={importActivity?.session.source_label}
            onImportAnotherSheet={importActivity?.session.can_rerun_workbook
              && ['completed', 'kept'].includes(importActivity.session.state)
              ? () => actSurface.openTableImportRerun(importActivity.importRef) : undefined}
            onOpenErrors={(importActivity?.session.problem_count ?? 0) > 0
              ? () => chrome.setActiveBottomDockTab('errors') : undefined}
            onDismiss={() => setSearchNoticeOpen(false)}
          />
        )}
      </div>
    )}
    <footer className="status-bar">
      <div className="status-left" data-testid="run-progress">
        {run ? (
          <>
            <button
              type="button"
              ref={watcherTriggerRef}
              className={`run-summary${runActive ? (runNoticeOpen ? ' run-summary-open' : '') : (watcherOpen ? ' run-summary-open' : '')}`}
              data-testid="run-watcher-toggle"
              data-run-status={run.status}
              data-run-completed-rows={run.completedRows}
              data-run-total-rows={run.totalRows}
              data-run-failed-rows={run.failedRows}
              title={runActive
                ? (runNoticeOpen ? 'Hide action progress' : 'Show action progress')
                : 'Run details'}
              aria-expanded={runActive ? runNoticeOpen : watcherOpen}
              onClick={() => {
                if (runActive) setRunNoticeOpen(!runNoticeOpen);
                else setCurrentWatcherOpen((o) => !o);
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
                  {activeOcrFiles(run).length === 0 && <span className="run-track">
                    <span
                      className="run-fill"
                      style={{ width: `${(100 * run.completedRows) / Math.max(1, run.totalRows)}%` }}
                    />
                  </span>}
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
                disabled={run.cancelRequested}
                onClick={() => onCancelRun(run.runId)}
              >
                <Square size={11} /> {run.cancelRequested ? 'Stopping…' : 'Cancel run'}
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
        ) : !searchPreparationNotice && (
          <span className="run-label muted">idle</span>
        )}
        {searchPreparationNotice && (
          <SearchActivityToggle
            notice={searchPreparationNotice}
            open={searchNoticeOpen}
            onToggle={() => setSearchNoticeOpen(!searchNoticeOpen)}
          />
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
        <span className="status-sep" />
        <button
          type="button"
          className="status-btn"
          data-testid="review-queue-button"
          onClick={onOpenReview}
          title="Open review queue"
        >
          <Inbox size={13} /> Review
          {reviewCount > 0 && <span className="badge">{reviewCount.toLocaleString()}</span>}
        </button>
      </div>
    </footer>
    </>
  );
}

function SearchActivityToggle({
  notice,
  open,
  onToggle,
}: {
  notice: SearchPreparationNoticeModel;
  open: boolean;
  onToggle(): void;
}) {
  return (
    <button
      type="button"
      className={`run-summary${open ? ' run-summary-open' : ''}`}
      data-testid="search-activity-toggle"
      aria-expanded={open}
      title={open ? 'Hide search preparation' : 'Show search preparation'}
      onClick={onToggle}
    >
      {notice.tone === 'error' || notice.tone === 'warning' ? (
        <AlertTriangle size={12} className="run-warning-icon" aria-hidden />
      ) : notice.tone === 'complete' ? (
        <span aria-hidden>✓</span>
      ) : (
        <span className="run-spinner" aria-hidden />
      )}
      <span className="run-label">{notice.title} · {notice.detail}</span>
      <ChevronUp size={12} className="run-chevron" aria-hidden />
    </button>
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
  const unit = run.actionKind === 'media.ocr' ? 'files' : 'rows';
  const files = activeOcrFiles(run);
  return (
    <div className="run-watcher run-watcher-inline" data-testid="run-watcher">
      <ProgressRow
        label={unit === 'files' ? 'OCR files' : 'Rows'}
        completed={run.completedRows}
        total={run.totalRows}
        state="active"
        status={rowsProgress(run)}
        ariaLabel={`${unit === 'files' ? 'OCR files' : 'Rows'} progress`}
      />
      <RunFileProgress files={files} />
      {run.failedRows > 0 && (
        <div className="active-run-failures">
          <span className="run-failed">{run.failedRows.toLocaleString()} failed</span>
          <RunWatcherErrors key={`${run.runId}:${run.failedRows}`} runId={run.runId} />
        </div>
      )}
    </div>
  );
}

function RunFileProgress({ files }: { files: ReturnType<typeof activeOcrFiles> }) {
  if (!files?.length) return null;
  return (
    <div className="run-file-progress-list">
      {files.map((file) => (
        <ProgressRow
          key={file.row_id}
          className="run-file-progress"
          layout="file"
          testId={`run-file-progress-${file.row_id}`}
          label={file.filename || `File row ${file.row_id}`}
          completed={file.completed}
          total={file.total}
          state="active"
          status={`${file.completed.toLocaleString()} / ${file.total.toLocaleString()} pages`}
          ariaLabel={`OCR pages for ${file.filename || `File row ${file.row_id}`}`}
        />
      ))}
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
  const files = activeOcrFiles(run);
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
            {run.cancelRequested && isActiveRunStatus(run.status) ? 'Stopping…' : runStatusLabel(run.status, run.failedRows)}
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
        <span className="prov-key">{run.actionKind === 'media.ocr' ? 'files' : 'rows'}</span>
        <span>
          {run.completedRows.toLocaleString()} / {run.totalRows.toLocaleString()}
          {files.length === 0 && ` (${pct.toFixed(0)}%)`}
        </span>
        <span className="prov-key">failed</span>
        <span className={run.failedRows > 0 ? 'run-failed' : undefined}>
          {run.failedRows.toLocaleString()}
        </span>
        <span className="prov-key">cost</span>
        <span>{formatUsd(run.costSoFar)}</span>
      </div>
      <RunFileProgress files={files} />
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
