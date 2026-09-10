import {
  useCallback,
  useEffect,
  useMemo,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useState,
} from 'react';
import { ChevronDown, ChevronUp, X } from 'lucide-react';
import type { ActionJob, RunProgress } from '../api/open';
import { runCompletedWithErrors } from '../runStatusModel';
import { useResizable } from '../components/useResizable';
import { navigate, useRoute } from '../routes';
import {
  isErrorJob,
  type DockRunSummary,
} from './dockJobSummary';
import { RowErrorGroupList } from './RowErrorGroupList';
import { AttemptReceiptList } from '../components/AttemptReceiptList';
import {
  ListDetailGrid,
  ListDetailHeader,
  WorkbenchListDetailPanel,
  type ListDetailColumn,
} from './WorkbenchListDetailPanel';
import type {
  WorkbenchResolvedLayoutContribution,
  WorkbenchResolvedLayoutRegion,
} from './layout';
import { workbenchNonHideableContributionIds } from './visibility';

export type DockActionJob = ActionJob & { progress?: RunProgress | null };

// The dock's active-tab fallback anchor: the jobs placement is non-hideable
// (WORKBENCH_VISIBILITY_HOST_POLICIES), so this placement always renders.
const BOTTOM_DOCK_FALLBACK_TAB_PLACEMENT_ID = 'jobs';

export interface WorkbenchBottomDockRenderContext {
  isActiveTab: boolean;
  focusTab(): void;
}

type BottomDockJobTableMode = 'jobs' | 'errors';

type BottomDockJobColumnKey =
  | 'job'
  | 'status'
  | 'run'
  | 'rows'
  | 'failed'
  | 'started'
  | 'finished'
  | 'attempts';

export interface WorkbenchProjectionStatus {
  activeContributionId: string;
  sheetId: string;
  columnId: string;
  columnName: string;
  count: number;
  validPoints: number;
  transient: boolean;
  generation: string;
  schema: string;
  backend: string;
}

// Monitor dock height seam: drag handle on the top edge, clamp 56–340px,
// default 118px, persisted in localStorage.
const BOTTOM_DOCK_HEIGHT_STORAGE_KEY = 'frisket:bottom-dock-height';
const BOTTOM_DOCK_COLLAPSED_STORAGE_KEY = 'frisket:bottom-dock-collapsed';
const BOTTOM_DOCK_DEFAULT_HEIGHT = 118;
const BOTTOM_DOCK_MIN_HEIGHT = 56;
const BOTTOM_DOCK_MAX_HEIGHT = 340;
const BOTTOM_DOCK_DETAIL_WIDTH_STORAGE_KEY = 'frisket:bottom-dock-detail-width';
const BOTTOM_DOCK_TABLE_WIDTH_STORAGE_PREFIX = 'frisket:bottom-dock-table-widths';
const BOTTOM_DOCK_TABLE_COLUMN_DEFAULT_WIDTHS: Record<BottomDockJobColumnKey, number> = {
  job: 260,
  status: 170,
  run: 84,
  rows: 110,
  failed: 92,
  started: 110,
  finished: 110,
  attempts: 104,
};

function bottomDockTableWidthStorageKey(mode: BottomDockJobTableMode): string {
  return `${BOTTOM_DOCK_TABLE_WIDTH_STORAGE_PREFIX}:${mode}`;
}

function formatDockTime(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function dockJobTitle(job: DockActionJob): string {
  return job.actionName || job.actionKind || job.kind;
}

function dockJobStatus(job: DockActionJob): string {
  if (runCompletedWithErrors(job.progress?.status, job.progress?.failedRows)) {
    return 'complete with errors';
  }
  if (job.progress?.status && job.progress.status !== 'complete') return job.progress.status;
  if (job.lease.leaseExpired && job.status === 'running') return 'stalled';
  if (job.status === 'done') return 'complete';
  return job.status;
}

function dockJobRows(job: DockActionJob): string {
  const total = job.progress?.totalRows;
  if (total == null) return '—';
  return `${job.progress?.completedRows.toLocaleString() ?? 0} / ${total.toLocaleString()}`;
}

function dockJobRunRef(job: DockActionJob): string {
  return job.runId ?? job.receiptId ?? '—';
}

function dockJobErrorMessage(job: DockActionJob): string {
  if (job.error) return job.error;
  if ((job.progress?.failedRows ?? 0) > 0) {
    return `${job.progress?.failedRows.toLocaleString()} failed row${job.progress?.failedRows === 1 ? '' : 's'}`;
  }
  return 'Worker lease expired before the run completed.';
}

function dockDetailRows(job: DockActionJob): Array<[string, string]> {
  return [
    ['Job id', String(job.jobId)],
    ['Run id', job.runId ?? '—'],
    ['Receipt id', job.receiptId ?? '—'],
    ['Status', dockJobStatus(job)],
    ['Action', job.actionKind ?? '—'],
    ['Rows', dockJobRows(job)],
    ['Failed rows', job.progress?.failedRows?.toLocaleString() ?? '—'],
    ['Attempts', `${job.attempts} / ${job.maxAttempts}`],
    ['Created', formatDockTime(job.timing.createdAt)],
    ['Started', formatDockTime(job.timing.startedAt)],
    ['Finished', formatDockTime(job.timing.finishedAt)],
    ['Locked by', job.lease.lockedBy ?? '—'],
    ['Locked at', formatDockTime(job.lease.lockedAt)],
    ['Lease expires', formatDockTime(job.lease.leaseExpiresAt)],
    // A typed halt behind a resumable 'cancelled' (e.g. a local model not yet
    // cached) — without this the run reads as a bare, reasonless cancel.
    ...(job.progress?.haltedReason
      ? ([['Stopped because', job.progress.haltedReason]] as Array<[string, string]>)
      : []),
  ];
}

function dockResultSummaryRows(job: DockActionJob): Array<[string, string]> {
  return Object.entries(job.resultSummary ?? {}).flatMap(([key, value]) => {
    if (value == null || value === '') return [];
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return [[key, String(value)]];
    }
    return [[key, JSON.stringify(value)]];
  });
}

// Precedent in web/src/components/action-panel/ActionForm.tsx's
// openCredentialSettings: a synthetic plugin-failure error row's detail pane
// deep-links into Settings → Plugins, riding the failed plugin's id along as
// ?plugin=<id> so the Plugins section can highlight/scroll to the exact row
// (PluginManager.tsx).
function openPluginSettingsFromError(job: DockActionJob, projectId: string | undefined) {
  navigate({ kind: 'settings', projectId, scope: 'project', section: 'plugins' });
  const pluginId =
    typeof job.resultSummary?.pluginId === 'string' ? job.resultSummary.pluginId : null;
  if (pluginId) {
    window.history.replaceState(
      window.history.state,
      '',
      `${window.location.pathname}?plugin=${encodeURIComponent(pluginId)}`,
    );
  }
}

// Ruling 4 (a retry is a resume): a run the recipe HALTED must keep its way
// back at the surface that reports the halt — before this the dock said
// "Stopped because …" and left the user to hunt for the column drawer. The
// resume routes through the workspace model's cost-gated run.backfill door
// (the same one every compliant retry surface uses): rows that already
// completed replay from durable results free; only never-completed rows buy.
// A promise_violation halt resumes through the SAME run.backfill door
// (ruling 4), but that door answers with a fresh confirmation of the changed
// terms, and this panel has nowhere to render one — so it points at the
// column drawer, which does, instead of offering a dead button.
function HaltedRunResumeSection({
  job,
  onResume,
}: {
  job: DockActionJob;
  onResume: (() => void) | null;
}) {
  const [requested, setRequested] = useState(false);
  const progress = job.progress;
  if (!progress?.haltedReason) return null;
  if (progress.completedRows === 0) {
    return (
      <section
        className="bottom-dock-detail-section"
        data-testid="bottom-dock-halt-restart"
      >
        <h4>Start again</h4>
        <p>Nothing was published. Start {dockJobTitle(job)} again from Actions.</p>
      </section>
    );
  }
  if (progress.haltedCode === 'promise_violation') {
    return (
      <section
        className="bottom-dock-detail-section"
        data-testid="bottom-dock-halt-reconfirm"
      >
        <h4>Resume</h4>
        <p>
          The terms changed since you approved this run, so resuming needs
          your consent to the new ones — which this panel cannot collect. Open
          the run&apos;s target column and choose &ldquo;Run missing
          cells&rdquo;: it shows you what changed before anything runs, and
          rows that already finished are kept.
        </p>
      </section>
    );
  }
  // The resume needs the run's target column, remembered from launch context;
  // a run restored without it (e.g. after a reload) points at the column
  // drawer's own resume door instead of offering a button it cannot honor.
  if (onResume === null || !progress.targetColumnId) {
    return (
      <section
        className="bottom-dock-detail-section"
        data-testid="bottom-dock-halt-resume-hint"
      >
        <h4>Resume</h4>
        <p>
          To resume, open the run&apos;s target column and choose &ldquo;Run
          missing cells&rdquo; — rows that already finished are kept.
        </p>
      </section>
    );
  }
  return (
    <section
      className="bottom-dock-detail-section"
      data-testid="bottom-dock-halt-resume"
    >
      <h4>Resume</h4>
      <p>
        {progress.completedRows.toLocaleString()} of{' '}
        {progress.totalRows.toLocaleString()} rows finished before the stop.
        Resuming keeps them — only the remaining rows run.
      </p>
      <button
        type="button"
        className="mini-btn"
        data-testid="bottom-dock-halt-resume-run"
        disabled={requested}
        onClick={() => {
          setRequested(true);
          onResume();
        }}
      >
        {requested ? 'Resume requested' : 'Resume run'}
      </button>
    </section>
  );
}

function renderJobDetail(
  job: DockActionJob,
  projectId?: string,
  onResumeHaltedRun?: (job: DockActionJob) => void,
): ReactNode {
  const summaryRows = dockResultSummaryRows(job);
  const rowErrors = job.progress?.rowErrors;
  const isPluginFailure = job.kind === 'plugin';
  return (
    <>
      <ListDetailHeader
        title={dockJobTitle(job)}
        status={dockJobStatus(job)}
        statusClassName={isErrorJob(job) ? 'bottom-dock-status-error' : undefined}
      />
      <ListDetailGrid rows={dockDetailRows(job)} />
      {/* key: `requested` is per-JOB state — without it, selecting another
          job would inherit the previous job's "Resume requested". */}
      <HaltedRunResumeSection
        key={job.jobId}
        job={job}
        onResume={onResumeHaltedRun ? () => onResumeHaltedRun(job) : null}
      />
      {isErrorJob(job) && (
        <section className="bottom-dock-detail-section">
          <h4>Error</h4>
          <pre>{dockJobErrorMessage(job)}</pre>
          {isPluginFailure && (
            <button
              type="button"
              className="btn bottom-dock-plugin-error-settings-link"
              data-testid="bottom-dock-plugin-error-settings-link"
              onClick={() => openPluginSettingsFromError(job, projectId)}
            >
              Open in Settings → Plugins
            </button>
          )}
        </section>
      )}
      {rowErrors && rowErrors.groups.length > 0 && (
        <section
          className="bottom-dock-detail-section"
          data-testid="bottom-dock-job-row-errors"
        >
          <h4>Row errors ({rowErrors.totalFailedRows.toLocaleString()})</h4>
          <RowErrorGroupList groups={rowErrors.groups} />
        </section>
      )}
      {summaryRows.length > 0 && (
        <section className="bottom-dock-detail-section" data-testid="bottom-dock-job-result-summary">
          <h4>Result</h4>
          <ListDetailGrid rows={summaryRows} />
        </section>
      )}
      {job.runId != null && (
        // The settlement receipt's UI seat: what this run was charged, in
        // what quantity, at what rate and under which pinned terms version —
        // or, for the free/local case, an honest "no charges" and no money
        // row at all.
        <section className="bottom-dock-detail-section" data-testid="bottom-dock-job-receipts">
          <h4>Charges</h4>
          <AttemptReceiptList runId={job.runId} testIdPrefix="bottom-dock-job-receipt" />
        </section>
      )}
    </>
  );
}

function jobColumn(
  key: BottomDockJobColumnKey,
  label: string,
  render: (job: DockActionJob) => ReactNode,
  cellClassName?: (job: DockActionJob) => string | undefined,
): ListDetailColumn<DockActionJob> {
  return { key, label, defaultWidth: BOTTOM_DOCK_TABLE_COLUMN_DEFAULT_WIDTHS[key], render, cellClassName };
}

const errorStatusClass = (job: DockActionJob) => (isErrorJob(job) ? 'bottom-dock-status-error' : undefined);

// The active-projection readout retired from a dock tab to a status-bar chip
// (components/StatusBar.tsx). The WorkbenchProjectionStatus interface below
// still models the chromeStore feed the chip renders; only the dock-tab
// panel component moved.

export function WorkbenchJobSplitPanel({
  jobs,
  loading,
  error,
  liveActionJobs,
  mode,
  onResumeHaltedRun,
}: {
  jobs: DockActionJob[];
  loading: boolean;
  error: string | null;
  liveActionJobs: { start(): void; dispose(): void };
  mode: BottomDockJobTableMode;
  /** Resume a HALTED run through the workspace model's cost-gated
   *  run.backfill door (ruling 4: a retry is a resume). Absent in hosts that
   *  cannot launch runs — the detail pane then explains instead of offering a
   *  dead button. */
  onResumeHaltedRun?: (job: DockActionJob) => void;
}) {
  useEffect(() => {
    liveActionJobs.start();
    return () => liveActionJobs.dispose();
  }, [liveActionJobs]);
  // For the plugin-error deep link (openPluginSettingsFromError): the bottom
  // dock only ever mounts inside a project workspace route, so this is
  // effectively always defined there; undefined degrades to a global-scope
  // settings navigate rather than throwing.
  const route = useRoute();
  const currentProjectId = route.kind === 'project' ? route.projectId : undefined;
  const columns = useMemo<Array<ListDetailColumn<DockActionJob>>>(() => (
    mode === 'errors'
      ? [
          jobColumn('job', 'Error', (job) => (
            <>
              <strong>{dockJobTitle(job)}</strong>
              <span className="bottom-dock-error-message">{dockJobErrorMessage(job)}</span>
            </>
          )),
          jobColumn('status', 'Status', dockJobStatus, errorStatusClass),
          jobColumn('run', 'Run', dockJobRunRef),
          jobColumn('finished', 'Finished', (job) => formatDockTime(job.timing.finishedAt)),
        ]
      : [
          jobColumn('job', 'Job', (job) => <strong>{dockJobTitle(job)}</strong>),
          jobColumn('status', 'Status', dockJobStatus, errorStatusClass),
          jobColumn('run', 'Run', dockJobRunRef),
          jobColumn('rows', 'Rows', dockJobRows),
          jobColumn('failed', 'Failed', (job) => job.progress?.failedRows?.toLocaleString() ?? '—', errorStatusClass),
          jobColumn('started', 'Started', (job) => formatDockTime(job.timing.startedAt ?? job.timing.createdAt)),
          jobColumn('finished', 'Finished', (job) => formatDockTime(job.timing.finishedAt)),
          jobColumn('attempts', 'Attempts', (job) => `${job.attempts} / ${job.maxAttempts}`),
        ]
  ), [mode]);
  const visibleJobs = mode === 'errors' ? jobs.filter(isErrorJob) : jobs;
  const emptyText = error ?? (
    loading
      ? mode === 'errors' ? 'Loading errors...' : 'Loading jobs...'
      : mode === 'errors' ? 'No failed run jobs' : 'No run jobs yet'
  );
  return (
    <WorkbenchListDetailPanel
      items={visibleJobs}
      getRowId={(job) => String(job.jobId)}
      columns={columns}
      renderDetail={(job) => renderJobDetail(job, currentProjectId, onResumeHaltedRun)}
      emptyText={emptyText}
      ariaLabel={mode === 'errors' ? 'Run errors' : 'Run jobs'}
      detailAriaLabel="Selected run details"
      tableWidthsStorageKey={bottomDockTableWidthStorageKey(mode)}
      detailWidthStorageKey={BOTTOM_DOCK_DETAIL_WIDTH_STORAGE_KEY}
      rowTestId={(job) => `bottom-dock-job-${job.jobId}`}
      rowData={(job) => ({
        'data-job-kind': job.kind,
        'data-action-kind': job.actionKind ?? '',
        'data-receipt-id': job.receiptId ?? '',
      })}
      columnHeaderTestId={(key) => `bottom-dock-column-${mode}-${key}`}
      columnResizeTestId={(key) => `bottom-dock-column-resize-${mode}-${key}`}
      splitResizeTestId="bottom-dock-split-resize"
    />
  );
}

// A dock tab is a resolved bottomDock:tab placement (identity: placementId).
// tabChrome.singleton (default true) keeps at most one tab per contribution;
// singleton: false renders one tab per placement.
function dockTabContributions(
  region: WorkbenchResolvedLayoutRegion,
): WorkbenchResolvedLayoutContribution[] {
  const seen = new Set<string>();
  const tabs: WorkbenchResolvedLayoutContribution[] = [];
  for (const contribution of region.contributions) {
    if (contribution.mode !== 'tab') continue;
    if (contribution.status !== 'enabled' && contribution.status !== 'disabled') continue;
    const singleton = contribution.tabChrome?.singleton !== false;
    if (singleton && seen.has(contribution.contributionId)) continue;
    seen.add(contribution.contributionId);
    tabs.push(contribution);
  }
  return tabs;
}

// Right-aligned live summary in the tab strip: the tri-state label plus,
// while work runs, an amber progress bar (aggregate percent when the active
// run reports row totals, otherwise indeterminate).
function DockRunSummaryView({ summary }: { summary?: DockRunSummary }) {
  const runningCount = summary?.runningCount ?? 0;
  const errored = summary?.anyErrored ?? false;
  const state = runningCount > 0
    ? 'running'
    : summary?.anyComplete
      ? (errored ? 'complete-with-errors' : 'complete')
      : 'idle';
  const label =
    state === 'running'
      ? `${runningCount.toLocaleString()} running`
      : state === 'complete-with-errors'
        ? 'complete with errors'
        : state === 'complete'
          ? 'all complete'
          : 'idle';
  const percent = summary?.percent ?? null;
  return (
    <output
      className="workbench-bottom-summary"
      data-testid="bottom-dock-run-summary"
      data-run-state={state}
      aria-live="polite"
    >
      {state === 'running' && (
        <span
          className={`workbench-bottom-progress${percent === null ? ' indeterminate' : ''}`}
          data-testid="bottom-dock-run-progressbar"
          role="progressbar"
          aria-label="Run progress"
          aria-valuemin={0}
          aria-valuemax={100}
          {...(percent !== null ? { 'aria-valuenow': Math.round(percent) } : {})}
        >
          <span
            className="workbench-bottom-progress-fill"
            style={percent !== null ? { width: `${percent}%` } : undefined}
          />
        </span>
      )}
      <span className="workbench-bottom-summary-label">{label}</span>
    </output>
  );
}

export function WorkbenchBottomDock({
  region,
  activeTabPlacementId,
  onSelectTab,
  onCloseTab,
  renderContribution,
  runSummary,
  tabBadges,
}: {
  region: WorkbenchResolvedLayoutRegion;
  activeTabPlacementId: string;
  onSelectTab: (contribution: WorkbenchResolvedLayoutContribution) => void;
  onCloseTab: (contribution: WorkbenchResolvedLayoutContribution) => void;
  renderContribution: (
    contribution: WorkbenchResolvedLayoutContribution,
    dock: WorkbenchBottomDockRenderContext,
  ) => ReactNode;
  /** Live run summary from the job store (running/complete/idle + progress). */
  runSummary?: DockRunSummary;
  /** Count chips keyed by placementId (e.g. the Errors tab's failure count). */
  tabBadges?: Record<string, number>;
}) {
  const {
    width: height,
    resizing,
    onResizeStart,
    onResizeKeyDown,
  } = useResizable({
    storageKey: BOTTOM_DOCK_HEIGHT_STORAGE_KEY,
    minWidth: BOTTOM_DOCK_MIN_HEIGHT,
    maxWidth: BOTTOM_DOCK_MAX_HEIGHT,
    defaultWidth: BOTTOM_DOCK_DEFAULT_HEIGHT,
    handleEdge: 'top',
  });
  // Minimize/dock parity with the Discover panel: collapsed = tab strip
  // only; clicking any tab (or the chevron) re-expands. Persisted.
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(BOTTOM_DOCK_COLLAPSED_STORAGE_KEY) === '1',
  );
  const setCollapsedPersist = useCallback((next: boolean) => {
    setCollapsed(next);
    localStorage.setItem(BOTTOM_DOCK_COLLAPSED_STORAGE_KEY, next ? '1' : '0');
  }, []);
  // An EXTERNAL tab focus (e.g. sheet-info's "View lineage" →
  // setActiveBottomDockTab) must reveal the dock's content, so a collapsed dock
  // expands when the active placement changes after mount (prev-props render
  // adjustment, not an effect). In-dock tab clicks already expand on click.
  //
  // This is react.dev's "storing previous props to adjust state during render"
  // idiom, NOT derived display state: prevActiveTab is never rendered — it is a
  // change DETECTOR, updated during render (below) whenever the prop moves, and
  // the value the dock actually shows (activeContribution) is derived fresh via
  // useMemo from activeTabPlacementId. no-derived-useState here is a false
  // positive — allowlisted in doctor.config.ts.
  const [prevActiveTab, setPrevActiveTab] = useState(activeTabPlacementId);
  if (prevActiveTab !== activeTabPlacementId) {
    setPrevActiveTab(activeTabPlacementId);
    if (collapsed) setCollapsedPersist(false);
  }
  const tabs = useMemo(() => dockTabContributions(region), [region]);
  const nonHideable = useMemo(() => workbenchNonHideableContributionIds(), []);
  // Fallback: an active placement that disappeared (plugin uninstalled or the
  // contribution hidden mid-session) falls back to the jobs anchor.
  const activeContribution = useMemo(
    () =>
      tabs.find((tab) => tab.placementId === activeTabPlacementId) ??
      tabs.find((tab) => tab.placementId === BOTTOM_DOCK_FALLBACK_TAB_PLACEMENT_ID) ??
      tabs[0] ??
      null,
    [activeTabPlacementId, tabs],
  );
  const onTabKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLButtonElement>, placementId: string) => {
      if (tabs.length === 0) return;
      const currentIndex = tabs.findIndex((tab) => tab.placementId === placementId);
      const lastIndex = tabs.length - 1;
      const nextIndex =
        event.key === 'ArrowRight'
          ? (currentIndex + 1) % tabs.length
          : event.key === 'ArrowLeft'
            ? (currentIndex + lastIndex) % tabs.length
            : event.key === 'Home'
              ? 0
              : event.key === 'End'
                ? lastIndex
                : -1;
      if (nextIndex < 0) return;
      event.preventDefault();
      const nextTab = tabs[nextIndex];
      onSelectTab(nextTab);
      requestAnimationFrame(() => {
        document
          .getElementById(`workbench-bottom-dock-tab-${nextTab.placementId}`)
          ?.focus();
      });
    },
    [onSelectTab, tabs],
  );

  return (
    <section
      className={`workbench-region workbench-bottom-dock${resizing ? ' workbench-bottom-dock-resizing' : ''}${collapsed ? ' workbench-bottom-dock-collapsed' : ''}`}
      style={collapsed ? undefined : { height }}
      data-testid="workbench-region-bottomDock"
      data-collapsed={collapsed ? 'true' : undefined}
      aria-label="Workbench bottom dock"
    >
      {!collapsed && (
        <button
          type="button"
          className="workbench-bottom-resize-handle"
          data-testid="bottom-dock-resize"
          aria-label="Resize bottom dock"
          onPointerDown={onResizeStart}
          onKeyDown={onResizeKeyDown}
        />
      )}
      <div
        className="workbench-bottom-tablist"
        data-testid="bottom-dock-tablist"
        role="tablist"
        aria-label="Operational output"
      >
        {tabs.map((tab) => {
          const isActive = activeContribution?.placementId === tab.placementId;
          const closeable =
            tab.tabChrome?.closeable === true && !nonHideable.has(tab.contributionId);
          const badge = tabBadges?.[tab.placementId] ?? 0;
          return (
            <span key={tab.placementId} className="workbench-bottom-tab-wrap">
              <button
                type="button"
                role="tab"
                className={`workbench-bottom-tab${isActive ? ' active' : ''}`}
                aria-selected={isActive}
                aria-controls="workbench-bottom-dock-panel"
                id={`workbench-bottom-dock-tab-${tab.placementId}`}
                data-testid={`bottom-dock-tab-${tab.placementId}`}
                data-contribution-id={tab.contributionId}
                data-placement-id={tab.placementId}
                data-status={tab.status}
                data-runtime-source={tab.runtimeSource}
                tabIndex={isActive ? 0 : -1}
                onClick={() => {
                  if (collapsed) setCollapsedPersist(false);
                  onSelectTab(tab);
                }}
                onKeyDown={(event) => onTabKeyDown(event, tab.placementId)}
              >
                <span className="workbench-bottom-tab-label">{tab.shortTitle ?? tab.title}</span>
                {badge > 0 && (
                  <span
                    className="workbench-bottom-tab-chip"
                    data-testid={`bottom-dock-tab-badge-${tab.placementId}`}
                  >
                    {badge.toLocaleString()}
                  </span>
                )}
              </button>
              {closeable && (
                <button
                  type="button"
                  className="workbench-bottom-tab-close icon-btn"
                  data-testid={`bottom-dock-tab-close-${tab.placementId}`}
                  aria-label={`Close ${tab.shortTitle ?? tab.title} tab`}
                  onClick={() => onCloseTab(tab)}
                >
                  <X size={11} />
                </button>
              )}
            </span>
          );
        })}
        <DockRunSummaryView summary={runSummary} />
        <button
          type="button"
          className="workbench-bottom-collapse icon-btn"
          data-testid="bottom-dock-collapse"
          aria-label={collapsed ? 'Expand the Monitor dock' : 'Minimize the Monitor dock'}
          aria-expanded={!collapsed}
          title={collapsed ? 'Expand' : 'Minimize'}
          onClick={() => setCollapsedPersist(!collapsed)}
        >
          {collapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      </div>
      {!collapsed && (
      <div
        id="workbench-bottom-dock-panel"
        className="workbench-bottom-panel"
        data-testid="bottom-dock-panel"
        role="tabpanel"
        aria-labelledby={
          activeContribution
            ? `workbench-bottom-dock-tab-${activeContribution.placementId}`
            : undefined
        }
        data-active-placement-id={activeContribution?.placementId ?? ''}
        data-active-contribution-id={activeContribution?.contributionId ?? ''}
      >
        {activeContribution
          ? renderContribution(activeContribution, {
              isActiveTab: true,
              focusTab: () => onSelectTab(activeContribution),
            })
          : null}
      </div>
      )}
    </section>
  );
}
