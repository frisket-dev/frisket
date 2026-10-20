import './Modal.css';
import { Button } from './Button';
import { ListEditorActions } from './ListEditorActions';
import { TextInput } from './FormControls';
import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from 'react';
import { PanelEmpty, PanelError, StatusChip } from './PanelPrimitives';
import { ChevronDown, ChevronRight, Play, RefreshCw, Watch } from 'lucide-react';
import {
  ApiError,
  type WatchInfo,
  type WatchRun,
  type WatchRunEvent,
  type WatchRunsPage,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { dispatchNotificationsChanged, useNotificationSummary } from './notifications/useNotificationSummary';
import { ConfirmDialog } from './Modal';
import { useWatchRunLinkHandle } from '../bind/useWatchRunLinkHandle';
import { useSelector } from '../bind/useSelector';
import type {
  PendingCreateDraft,
  WatchActionOutcome,
  WatchActionIntent,
  WatchRunLink,
} from '../state/watchRunLinkStore';
import { useEditionModule } from '../editions/module';
import './WatchesPanel.css';

type RunsByWatch = Record<number, WatchRunsPage>;
type EventsByRun = Record<number, WatchRunEvent[]>;
type WatchNotificationCounts = Record<number, number>;
interface WatchesPanelState {
  items: WatchInfo[] | null;
  runsByWatch: RunsByWatch;
  eventsByRun: EventsByRun;
  highlightedEventsByRun: Record<number, number[]>;
  expanded: Record<number, boolean>;
  listLoading: boolean;
  creating: boolean;
  error: string | null;
  errorCode: string | null;
  status: string | null;
  focusWatchId: number | null;
  focusPanel: boolean;
  mutationErrors: Record<number, string>;
  notificationSetupNotice: string | null;
}

type WatchesPanelPatch =
  | Partial<WatchesPanelState>
  | ((state: WatchesPanelState) => Partial<WatchesPanelState>);

const initialWatchesPanelState: WatchesPanelState = {
  items: null,
  runsByWatch: {},
  eventsByRun: {},
  highlightedEventsByRun: {},
  expanded: {},
  listLoading: false,
  creating: false,
  error: null,
  errorCode: null,
  status: null,
  focusWatchId: null,
  focusPanel: false,
  mutationErrors: {},
  notificationSetupNotice: null,
};

const NOTIFICATION_SETUP_GUIDANCE = 'You do not have Slack or email notifications set up. Visit settings to set them up.';
const WatchIcon = Watch;

function reduceWatchesPanelState(
  state: WatchesPanelState,
  patch: WatchesPanelPatch,
): WatchesPanelState {
  return { ...state, ...(typeof patch === 'function' ? patch(state) : patch) };
}

function watchSummary(watch: WatchInfo): string {
  const kind = String(watch.query?.kind ?? '');
  if (kind === 'fts') return `Search "${String(watch.query?.q ?? '')}"`;
  if (kind === 'filter') return 'Saved filter';
  if (kind === 'embedding_similarity') return 'Rows similar to a row';
  return kind || 'Watch';
}

// An embedding_similarity watch blocks until its index is refreshed — a
// manual stale/incomplete state needs a refresh.
function blockedEmbeddingIndexId(watch: WatchInfo): string | null {
  if (String(watch.query?.kind ?? '') !== 'embedding_similarity') return null;
  const code = watch.latestRun?.errorCode ?? '';
  if (code !== 'embedding_index_incomplete' && code !== 'embedding_index_stale') {
    return null;
  }
  const indexId = watch.query?.embedding_index_id;
  return typeof indexId === 'string' ? indexId : null;
}

function runSummary(run: WatchRun | null): string {
  if (!run) return 'Never run';
  if (run.status === 'error') return run.errorCode ?? run.error ?? 'Run failed';
  return `${run.matchedRows.toLocaleString()} matched · ${run.newRows.toLocaleString()} new`;
}

function failedWatchActionOutcome(error: Error): WatchActionOutcome {
  const fallbackCode = Reflect.get(error, 'code');
  const code = error instanceof ApiError
    ? error.code ?? null
    : typeof fallbackCode === 'string'
      ? fallbackCode
      : null;
  return {
    status: null,
    error: error.message,
    errorCode: code,
    watch: null,
    focusWatchId: null,
  };
}

/**
 * Manual watchlist runner. This intentionally does not know about schedules or
 * notification channels; it only lists promoted queries and records run history.
 */
function useWatchesPanelController(canEdit: boolean) {
  const { projectApi, chromePreferences: { projectId } } = useWorkspaceStores();
  const notificationSummary = useNotificationSummary();
  const watchRunLinkHandle = useWatchRunLinkHandle();
  const [panelState, setPanelState] = useReducer(
    reduceWatchesPanelState,
    initialWatchesPanelState,
  );
  const {
    items,
    runsByWatch,
    eventsByRun,
    highlightedEventsByRun,
    expanded,
    listLoading,
    creating,
    error,
    errorCode,
    status,
    focusWatchId,
    mutationErrors,
    notificationSetupNotice,
  } = panelState;
  const watchListGeneration = useRef(0);

  const invalidateWatchList = useCallback(() => {
    watchListGeneration.current += 1;
    setPanelState({ listLoading: false });
  }, []);

  const watchNotificationCounts: WatchNotificationCounts = {};
  for (const item of notificationSummary?.bySourceRef ?? []) {
    if (item.sourceKind !== 'watch') continue;
    const watchId = Number(item.sourceRef.watch_id);
    if (Number.isInteger(watchId) && watchId > 0) watchNotificationCounts[watchId] = item.unseen;
  }

  const load = useCallback(() => {
    const generation = ++watchListGeneration.current;
    setPanelState({
      listLoading: true,
      error: null,
      errorCode: null,
    });
    void projectApi
      .listWatches()
      .then((watches) => {
        if (watchListGeneration.current === generation) setPanelState({ items: watches });
      })
      .catch((e: Error) => {
        if (watchListGeneration.current === generation) setPanelState({ error: e.message });
      })
      .finally(() => {
        if (watchListGeneration.current === generation) {
          setPanelState({ listLoading: false });
        }
      });
  }, [projectApi]);

  const loadRuns = useCallback((watchId: number) => {
    return projectApi
      .getWatchRuns(watchId, 0, 20)
      .then((page) => {
        setPanelState((current) => ({
          runsByWatch: { ...current.runsByWatch, [watchId]: page },
        }));
        return page;
      });
  }, [projectApi]);

  const loadRunEvents = useCallback((
    watchId: number,
    runId: number,
    eventIds: number[] = [],
  ) => {
    return projectApi
      .getWatchRunEvents(watchId, runId, 0, 50)
      .then((page) => {
        setPanelState((current) => ({
          eventsByRun: { ...current.eventsByRun, [runId]: page.events },
          highlightedEventsByRun: {
            ...current.highlightedEventsByRun,
            [runId]: eventIds,
          },
        }));
        return page;
      });
  }, [projectApi]);

  useEffect(() => {
    const timer = window.setTimeout(load, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    const onChanged = () => load();
    window.addEventListener('frisket:watches-changed', onChanged);
    return () => window.removeEventListener('frisket:watches-changed', onChanged);
  }, [load]);

  const applyWatchRunLink = useCallback((link: WatchRunLink) => {
    const watchId = Number(link.watchId);
    const runId = Number(link.runId);
    if (!Number.isInteger(watchId) || !Number.isInteger(runId)) return;
    const eventIds = Array.isArray(link.eventIds) ? link.eventIds : [];
    setPanelState((current) => ({
      expanded: { ...current.expanded, [watchId]: true },
    }));
    load();
    void loadRuns(watchId)
      .then(() => loadRunEvents(watchId, runId, eventIds))
      .catch((e: Error) => setPanelState({ error: e.message }));
  }, [load, loadRunEvents, loadRuns]);

  // A notification's "open source" click writes
  // watchRunLinkStore's pendingWatchRunLink instead of dispatching a live event —
  // the Discover-tab mount may be unmounted at write time and applies the
  // intent on its later mount. Read the store FRESH (watchRunLinkHandle.store.get(), not
  // the closed-over selector value) so a StrictMode double-fired effect sees
  // the slot already acked and is a no-op the second time.
  // NO blind clear: only the target this panel owns ('watches') acks.
  const pendingWatchRunLink = useSelector(
    watchRunLinkHandle.store,
    (s) => s.pendingWatchRunLink,
  );
  const pendingCreateDraft = useSelector(
    watchRunLinkHandle.store,
    (s) => s.pendingCreateDraft,
  );
  const pendingCreateAction = useSelector(
    watchRunLinkHandle.store,
    (s) => s.pendingCreateAction,
  );
  const watchActions = useSelector(
    watchRunLinkHandle.store,
    (s) => s.watchActions,
  );
  useEffect(() => {
    const pending = watchRunLinkHandle.store.get().pendingWatchRunLink;
    if (!pending || pending.target !== 'watches') return;
    applyWatchRunLink(pending.link);
    watchRunLinkHandle.ackPendingWatchRunLink();
  }, [pendingWatchRunLink, watchRunLinkHandle, applyWatchRunLink]);
  useEffect(() => {
    for (const action of Object.values(watchRunLinkHandle.store.get().watchActions)) {
      if (action.phase !== 'settled' || !action.outcome) continue;
      invalidateWatchList();
      setPanelState((current) => {
        const outcome = action.outcome!;
        const lifecycleError = outcome.error !== null && action.intent !== 'run' && action.intent !== 'refresh_run';
        const items = outcome.error !== null ? current.items : outcome.watch === null
          ? (current.items ?? []).filter((watch) => watch.id !== action.watchId)
          : [...(current.items ?? []).filter((watch) => watch.id !== outcome.watch!.id), outcome.watch!];
        return {
          items,
          status: outcome.status,
          error: lifecycleError ? null : outcome.error,
          errorCode: lifecycleError ? null : outcome.errorCode,
          focusWatchId: outcome.focusWatchId,
          focusPanel: action.intent === 'delete'
            && outcome.error === null
            && outcome.focusWatchId === null,
          mutationErrors: lifecycleError
            ? { ...current.mutationErrors, [action.watchId]: outcome.error ?? '' }
            : current.mutationErrors,
        };
      });
      watchRunLinkHandle.ackWatchAction(action);
    }
  }, [invalidateWatchList, watchActions, watchRunLinkHandle]);
  useEffect(() => {
    const action = watchRunLinkHandle.store.get().pendingCreateAction;
    if (!action || action.phase !== 'settled' || !action.outcome) return;
    invalidateWatchList();
    const outcome = action.outcome;
    setPanelState((current) => ({
      items: outcome.error === null && outcome.watch
        ? [...(current.items ?? []).filter((watch) => watch.id !== outcome.watch!.id), outcome.watch]
        : current.items,
      status: outcome.status,
      error: outcome.error,
      errorCode: outcome.errorCode,
      focusWatchId: outcome.focusWatchId,
      creating: false,
      notificationSetupNotice: outcome.error === null && outcome.watch
        ? NOTIFICATION_SETUP_GUIDANCE
        : current.notificationSetupNotice,
    }));
    watchRunLinkHandle.ackCreateWatch(action);
  }, [invalidateWatchList, pendingCreateAction, watchRunLinkHandle]);

  const runWatch = useCallback((watchId: number) => {
    if (!canEdit) return;
    const claim = watchRunLinkHandle.claimWatchAction(watchId, 'run');
    if (!claim) return;
    invalidateWatchList();
    setPanelState({ error: null, errorCode: null, status: 'Running Watch…' });
    let outcome!: WatchActionOutcome;
    void projectApi
      .runWatch(watchId)
      .then((result) => {
        outcome = {
          status: `Finished running ${result.watch.name}.`,
          error: null,
          errorCode: null,
          watch: result.watch,
          focusWatchId: watchId,
        };
        dispatchNotificationsChanged(projectId);
        if (expanded[watchId]) {
          void loadRuns(watchId).catch((e: Error) => setPanelState({ error: e.message }));
        }
      })
      .catch((e: Error) => { outcome = failedWatchActionOutcome(e); })
      .finally(() => {
        // A list GET started while this mutation was in flight must not
        // publish after the direct mutation result above.
        invalidateWatchList();
        watchRunLinkHandle.settleWatchAction(claim, outcome);
        window.dispatchEvent(new Event('frisket:watches-changed'));
      });
  }, [canEdit, expanded, invalidateWatchList, projectId, loadRuns, projectApi, watchRunLinkHandle]);

  const updateWatch = useCallback((
    watchId: number,
    patch: { name?: string; enabled?: boolean },
  ) => {
    if (!canEdit) return Promise.resolve(false);
    const intent: WatchActionIntent = patch.name === undefined
      ? (patch.enabled ? 'resume' : 'pause')
      : 'rename';
    const claim = watchRunLinkHandle.claimWatchAction(watchId, intent);
    if (!claim) return Promise.resolve(false);
    invalidateWatchList();
    setPanelState((current) => ({
      error: null,
      errorCode: null,
      status: intent === 'rename' ? 'Renaming Watch…' : intent === 'pause' ? 'Pausing Watch…' : 'Resuming Watch…',
      mutationErrors: { ...current.mutationErrors, [watchId]: '' },
    }));
    let outcome!: WatchActionOutcome;
    return projectApi
      .updateWatch(watchId, patch)
      .then((updated) => {
        outcome = {
          status: patch.name === undefined
            ? `${updated.name} is now ${updated.enabled ? 'active' : 'paused'}.`
            : `Renamed Watch to ${updated.name}.`,
          error: null,
          errorCode: null,
          watch: updated,
          focusWatchId: watchId,
        };
        return true;
      })
      .catch((e: Error) => {
        outcome = failedWatchActionOutcome(e);
        return false;
      })
      .finally(() => {
        invalidateWatchList();
        watchRunLinkHandle.settleWatchAction(claim, outcome);
        window.dispatchEvent(new Event('frisket:watches-changed'));
      });
  }, [canEdit, invalidateWatchList, projectApi, watchRunLinkHandle]);

  const deleteWatch = useCallback((watchId: number, focusAfterDeleteId: number | null) => {
    if (!canEdit) return Promise.resolve(false);
    const claim = watchRunLinkHandle.claimWatchAction(watchId, 'delete');
    if (!claim) return Promise.resolve(false);
    invalidateWatchList();
    setPanelState((current) => ({
      error: null,
      errorCode: null,
      status: 'Deleting Watch…',
      mutationErrors: { ...current.mutationErrors, [watchId]: '' },
    }));
    let outcome!: WatchActionOutcome;
    return projectApi
      .deleteWatch(watchId)
      .then(() => {
        outcome = {
          status: 'Watch deleted.',
          error: null,
          errorCode: null,
          watch: null,
          focusWatchId: focusAfterDeleteId,
        };
        dispatchNotificationsChanged(projectId);
        return true;
      })
      .catch((e: Error) => {
        outcome = failedWatchActionOutcome(e);
        return false;
      })
      .finally(() => {
        invalidateWatchList();
        watchRunLinkHandle.settleWatchAction(claim, outcome);
        window.dispatchEvent(new Event('frisket:watches-changed'));
      });
  }, [canEdit, invalidateWatchList, projectId, projectApi, watchRunLinkHandle]);

  // Refresh a blocked embedding watch's index, then re-run the watch.
  const refreshWatchIndex = useCallback((watchId: number, indexId: string) => {
    if (!canEdit) return;
    const claim = watchRunLinkHandle.claimWatchAction(watchId, 'refresh_run');
    if (!claim) return;
    invalidateWatchList();
    setPanelState({ error: null, errorCode: null, status: 'Refreshing index…' });
    let outcome!: WatchActionOutcome;
    void projectApi
      .refreshEmbeddingIndex(indexId, 'incremental')
      .then(() => projectApi.runWatch(watchId))
      .then((result) => {
        outcome = {
          status: `Finished running ${result.watch.name}.`,
          error: null,
          errorCode: null,
          watch: result.watch,
          focusWatchId: watchId,
        };
        dispatchNotificationsChanged(projectId);
        if (expanded[watchId]) {
          void loadRuns(watchId).catch((e: Error) => setPanelState({ error: e.message }));
        }
      })
      .catch((e: Error) => { outcome = failedWatchActionOutcome(e); })
      .finally(() => {
        invalidateWatchList();
        watchRunLinkHandle.settleWatchAction(claim, outcome);
        window.dispatchEvent(new Event('frisket:watches-changed'));
      });
  }, [canEdit, expanded, invalidateWatchList, projectId, loadRuns, projectApi, watchRunLinkHandle]);

  const toggleRuns = useCallback((watchId: number) => {
    const nextOpen = !expanded[watchId];
    setPanelState((current) => ({
      expanded: { ...current.expanded, [watchId]: nextOpen },
    }));
    if (!nextOpen || runsByWatch[watchId]) return;
    setPanelState({ error: null });
    void loadRuns(watchId)
      .catch((e: Error) => setPanelState({ error: e.message }));
  }, [expanded, loadRuns, runsByWatch]);

  const createWatch = useCallback(() => {
    // Validate the current draft before taking the store-owned claim. The
    // claim is synchronous, so a remounted panel or a rapid second click
    // cannot submit this revision twice.
    const draft = watchRunLinkHandle.store.get().pendingCreateDraft;
    const name = draft?.name.trim();
    if (!canEdit || !draft || !name) return;
    const action = watchRunLinkHandle.claimCreateWatch();
    if (!action) return;

    invalidateWatchList();
    setPanelState({ creating: true, error: null, errorCode: null, status: 'Creating Watch…' });
    void projectApi
      .createWatch({ name, query: draft.query, scope: draft.scope })
      .then((watch) => {
        watchRunLinkHandle.settleCreateWatch(action, {
          status: `Created Watch ${watch.name}.`, error: null, errorCode: null, watch, focusWatchId: watch.id,
        });
      })
      .catch((e: Error) => {
        watchRunLinkHandle.settleCreateWatch(action, failedWatchActionOutcome(e));
      });
  }, [canEdit, invalidateWatchList, projectApi, watchRunLinkHandle]);

  const cancelCreateWatch = useCallback(() => {
    if (!canEdit || watchRunLinkHandle.store.get().pendingCreateAction?.phase === 'pending') return;
    watchRunLinkHandle.clearPendingCreateDraft();
  }, [canEdit, watchRunLinkHandle]);

  const updateCreateDraft = useCallback((
    patch: Pick<PendingCreateDraft, 'name'>,
  ) => {
    if (!canEdit || watchRunLinkHandle.store.get().pendingCreateAction?.phase === 'pending') return;
    watchRunLinkHandle.updatePendingCreateDraft(patch);
  }, [canEdit, watchRunLinkHandle]);

  const watchNotificationTotal = Object.values(watchNotificationCounts).reduce(
    (sum, count) => sum + count,
    0,
  );

  return {
    listLoading,
    cancelCreateWatch,
    createWatch,
    creating: creating || pendingCreateAction?.phase === 'pending',
    error,
    errorCode,
    eventsByRun,
    expanded,
    highlightedEventsByRun,
    items,
    load,
    pendingCreateDraft,
    refreshWatchIndex,
    runWatch,
    runsByWatch,
    toggleRuns,
    updateCreateDraft,
    watchNotificationCounts,
    watchNotificationTotal,
    mutationErrors,
    pendingActions: Object.fromEntries(Object.entries(watchActions).map(([id, action]) => [id, action.intent])),
    status,
    focusWatchId,
    clearFocusWatch: () => setPanelState({ focusWatchId: null }),
    focusPanel: panelState.focusPanel,
    clearFocusPanel: () => setPanelState({ focusPanel: false }),
    updateWatch,
    deleteWatch,
    notificationSetupNotice,
    dismissNotificationSetupNotice: () => setPanelState({ notificationSetupNotice: null }),
  };
}

// The Discover tab already labels this panel "Watches" — no second collapse
// level, no toggle button. This slim static row only exists to keep the
// notification/item count badges visible somewhere once their old toggle
// header is gone; it renders nothing when there is nothing to show.
//
function WatchesPanelHeader({
  itemCount,
  notificationTotal,
}: {
  itemCount: number | null;
  notificationTotal: number;
}) {
  if (notificationTotal <= 0 && !(itemCount !== null && itemCount > 0)) return null;
  return (
    <div className="control-row" data-align="end" data-testid="watches-header">
      {notificationTotal > 0 && (
        <StatusChip tone="info" size="sm" testId="watches-notification-count">
          {notificationTotal}
        </StatusChip>
      )}
      {itemCount !== null && itemCount > 0 && <StatusChip tone="neutral" size="sm">{itemCount}</StatusChip>}
    </div>
  );
}

function WatchCreateComposer({
  creating,
  draft,
  onCancel,
  onCreate,
  onUpdate,
}: {
  creating: boolean;
  draft: PendingCreateDraft;
  onCancel(): void;
  onCreate(): void;
  onUpdate(patch: Pick<PendingCreateDraft, 'name'>): void;
}) {
  const trimmedName = draft.name.trim();
  return (
    <div className="panel-card" data-testid="watch-create-composer">
      <div>
        <div className="panel-item-meta">New Watch</div>
        <strong className="panel-item-title">Keep an eye on new matches.</strong>
      </div>
      <label className="form-field">
        <span className="form-label">Watch name</span>
        <TextInput
          autoFocus

          data-testid="watch-create-name"
          aria-label="Watch name"
          aria-describedby={trimmedName ? undefined : 'watch-create-name-error'}
          value={draft.name}
          disabled={creating}
          onChange={(event) => onUpdate({ name: event.target.value })}
        />
      </label>
      {!trimmedName && <span id="watch-create-name-error" data-testid="watch-create-name-error">Enter a name to continue.</span>}
      <div className="panel-notice">
        {NOTIFICATION_SETUP_GUIDANCE}
      </div>
      <div className="form-actions">
        <ListEditorActions testId="create-watch-button" cancelTestId="cancel"
          icon={WatchIcon} saveLabel={creating ? 'Creating…' : 'Create new watch'}
          busy={creating} saveDisabled={!trimmedName} onSave={onCreate} onCancel={onCancel} />
      </div>
    </div>
  );
}

function WatchRunEvents({
  events,
  highlightedEventIds,
}: {
  events: WatchRunEvent[] | undefined;
  highlightedEventIds: number[];
}) {
  if (!events) return null;
  return (
    <ul className="panel-list watch-run-events" data-testid="watch-run-events">
      {events.map((event) => (
        <li
          className={`control-row panel-item-meta${highlightedEventIds.includes(event.id) ? ' highlighted' : ''}`}
          key={event.id}
        >
          <strong>{event.eventKind}</strong>
          <span>{event.snippet ?? event.subjectKind}</span>
        </li>
      ))}
    </ul>
  );
}

function WatchRunHistory({
  highlightedEventsByRun,
  eventsByRun,
  page,
}: {
  highlightedEventsByRun: Record<number, number[]>;
  eventsByRun: EventsByRun;
  page: WatchRunsPage | undefined;
}) {
  return (
    <ul className="panel-list" data-testid="watch-run-history">
      {!page || page.runs.length === 0 ? (
        <li className="panel-empty">No runs yet.</li>
      ) : (
        page.runs.map((run) => (
          <li className="panel-list" key={run.id}>
            <div className="control-row">
              <StatusChip tone={run.status === 'ok' ? 'success' : run.status === 'error' ? 'error' : 'neutral'} size="sm" uppercase>{run.status}</StatusChip>
              <span className="panel-item-meta panel-item-main">
                {run.status === 'error'
                  ? (run.errorCode ?? run.error ?? 'Run failed')
                  : `${run.matchedRows.toLocaleString()} matched · ${run.newRows.toLocaleString()} new`}
              </span>
            </div>
            <WatchRunEvents
              events={eventsByRun[run.id]}
              highlightedEventIds={highlightedEventsByRun[run.id] ?? []}
            />
          </li>
        ))
      )}
    </ul>
  );
}

function WatchItem({
  pendingAction,
  canEdit,
  eventsByRun,
  expanded,
  highlightedEventsByRun,
  notificationCount,
  page,
  watch,
  onRefreshIndex,
  onRefreshWatches,
  onRun,
  onToggleRuns,
  onUpdateWatch,
  onDeleteWatch,
  focusAfterDeleteId,
  shouldFocus,
  onFocused,
  mutationError,
}: {
  pendingAction: WatchActionIntent | undefined;
  canEdit: boolean;
  eventsByRun: EventsByRun;
  expanded: boolean;
  highlightedEventsByRun: Record<number, number[]>;
  notificationCount: number;
  page: WatchRunsPage | undefined;
  watch: WatchInfo;
  onRefreshIndex(watchId: number, indexId: string): void;
  onRefreshWatches(): void;
  onRun(watchId: number): void;
  onToggleRuns(watchId: number): void;
  onUpdateWatch(watchId: number, patch: { name?: string; enabled?: boolean }): Promise<boolean>;
  onDeleteWatch(watchId: number, focusAfterDeleteId: number | null): Promise<boolean>;
  focusAfterDeleteId: number | null;
  shouldFocus: boolean;
  onFocused(): void;
  mutationError: string | undefined;
}) {
  const RunChevron = expanded ? ChevronDown : ChevronRight;
  const blockedIndexId = blockedEmbeddingIndexId(watch);
  const { WatchControlSlot } = useEditionModule();
  const { projectId } = useWorkspaceStores().chromePreferences;
  const itemRef = useRef<HTMLLIElement>(null);
  const deleteButtonRef = useRef<HTMLButtonElement>(null);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState(watch.name);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const trimmedRename = renameValue.trim();
  const isPending = pendingAction !== undefined;
  const isRunning = pendingAction === 'run';
  const isRenaming = pendingAction === 'rename';
  const isPausing = pendingAction === 'pause';
  const isResuming = pendingAction === 'resume';
  const isDeleting = pendingAction === 'delete';
  const isRefreshingIndex = pendingAction === 'refresh_run';
  const pendingActionLabel = pendingAction === 'rename'
    ? 'Renaming…'
    : pendingAction === 'pause'
      ? 'Pausing…'
      : pendingAction === 'resume'
        ? 'Resuming…'
        : pendingAction === 'delete'
          ? 'Deleting…'
          : null;
  useEffect(() => {
    if (!shouldFocus) return;
    itemRef.current?.focus();
    onFocused();
  }, [onFocused, shouldFocus]);

  const closeDeleteDialog = () => {
    setConfirmingDelete(false);
    window.requestAnimationFrame(() => deleteButtonRef.current?.focus());
  };

  const confirmDelete = () => {
    void onDeleteWatch(watch.id, focusAfterDeleteId);
  };
  return (
    <li ref={itemRef} className="panel-card watch-item" data-testid="watch-item" tabIndex={-1}>
      <div className="control-row">
        <span className="panel-item-title panel-item-main">{watch.name}</span>
        {notificationCount > 0 && (
          <StatusChip tone="info" size="sm" testId="watch-notification-count">
            {notificationCount}
          </StatusChip>
        )}
        {canEdit && (
          <button
            type="button"
            className="icon-btn"
            // A Watch run always performs a complete fresh evaluation.
            title={`Run ${watch.name} — manual evaluation; a full fresh check, not a resume`}
            aria-label={isRunning ? `Running ${watch.name}…` : `Run ${watch.name} — manual evaluation; a full fresh check, not a resume`}
            aria-busy={isRunning}
            data-testid="run-watch"
            disabled={isPending}
            onClick={() => onRun(watch.id)}
          >
            {isRunning ? (
              <RefreshCw size={13} className="spin" />
            ) : (
              <Play size={13} />
            )}
          </button>
        )}
      </div>
      <div className="panel-item-description">
        {watchSummary(watch)}
      </div>
      {!watch.enabled && <div className="control-row"><StatusChip tone="neutral" size="sm" testId="watch-status">Paused</StatusChip></div>}
      <div className="control-row panel-item-meta" data-testid="watch-latest-run">
        {runSummary(watch.latestRun)}
        {canEdit && blockedIndexId && (
          <Button
            type="button"
            data-testid={`watch-refresh-index-${watch.id}`}
            // Ruling 4 honesty: this refreshes the embedding index AND then
            // runs the whole watch again — two purchases, named up front.
            title="Refresh the embedding index, then run the whole watch again"
            aria-busy={isRefreshingIndex}
            disabled={isPending}
            onClick={() => onRefreshIndex(watch.id, blockedIndexId)}
          >
            <RefreshCw size={11} className={isRefreshingIndex ? 'spin' : undefined} /> {isRefreshingIndex ? 'Refreshing index…' : 'Refresh index & re-run'}
          </Button>
        )}
      </div>
      {canEdit && (
        <div className="control-row">
          {pendingActionLabel && (
            <span data-testid="watch-pending-action" role="status">{pendingActionLabel}</span>
          )}
          {renaming ? (
            <>
              <TextInput
                data-testid="watch-rename-input"
                aria-label={`Rename ${watch.name}`}
                aria-describedby={trimmedRename ? undefined : `watch-rename-error-${watch.id}`}
                disabled={isPending}
                value={renameValue}
                onChange={(event) => setRenameValue(event.target.value)}
              />
              {!trimmedRename && <span id={`watch-rename-error-${watch.id}`} data-testid="watch-rename-error">Enter a name to continue.</span>}
              <ListEditorActions testId="save-watch-rename" saveLabel={isRenaming ? 'Renaming…' : 'Save rename'}
                busy={isRenaming} disabled={isPending} saveDisabled={!trimmedRename}
                onSave={() => { void onUpdateWatch(watch.id, { name: trimmedRename }).then((saved) => { if (saved) setRenaming(false); }); }}
                onCancel={() => setRenaming(false)} />
            </>
          ) : (
            <Button type="button" data-testid="rename-watch" disabled={isPending} onClick={() => { setRenameValue(watch.name); setRenaming(true); }}>Rename</Button>
          )}
          <Button type="button" data-testid={watch.enabled ? 'pause-watch' : 'resume-watch'} aria-busy={isPausing || isResuming} disabled={isPending} onClick={() => { void onUpdateWatch(watch.id, { enabled: !watch.enabled }); }}>{isPausing ? 'Pausing…' : isResuming ? 'Resuming…' : watch.enabled ? 'Pause' : 'Resume'}</Button>
          <Button ref={deleteButtonRef} type="button" className="danger" data-testid="delete-watch" disabled={isPending} onClick={() => setConfirmingDelete(true)}>Delete</Button>
        </div>
      )}
      {mutationError && !confirmingDelete && <PanelError testId="watch-mutation-error">{mutationError}</PanelError>}
      {confirmingDelete && (
        <ConfirmDialog title={`Delete ${watch.name}?`} busy={isDeleting} danger
          testId="delete-watch-confirmation" confirmTestId="confirm-delete-watch"
          confirmLabel={isDeleting ? 'Deleting…' : 'Delete Watch'}
          onCancel={closeDeleteDialog} onConfirm={confirmDelete}>
          <p className="modal-body-text">
            Deleting this Watch means its run history and in-app notifications will be deleted; already-sent email or external notifications cannot be recalled.
          </p>
          {mutationError && <PanelError testId="watch-mutation-error">{mutationError}</PanelError>}
        </ConfirmDialog>
      )}
      {canEdit && WatchControlSlot && (
        <WatchControlSlot
          projectId={projectId}
          watch={watch}
          refreshWatches={onRefreshWatches}
        />
      )}
      <div className="control-row panel-item-meta">
        <Button
          type="button"
          variant="text"
          data-testid="toggle-watch-runs"
          aria-expanded={expanded}
          onClick={() => onToggleRuns(watch.id)}
        >
          <RunChevron size={11} /> Runs
        </Button>
      </div>
      {expanded && (
        <WatchRunHistory
          eventsByRun={eventsByRun}
          highlightedEventsByRun={highlightedEventsByRun}
          page={page}
        />
      )}
    </li>
  );
}

function WatchesPanelBody({
  listLoading,
  pendingActions,
  canEdit,
  canCreateFromCurrentView,
  onCreateFromCurrentView,
  creating,
  draft,
  error,
  eventsByRun,
  expanded,
  highlightedEventsByRun,
  items,
  onRefreshIndex,
  onRefreshWatches,
  onRun,
  onToggleRuns,
  onCancelCreate,
  onCreate,
  onUpdateCreate,
  onUpdateWatch,
  onDeleteWatch,
  mutationErrors,
  focusWatchId,
  onWatchFocused,
  notificationSetupNotice,
  onDismissNotificationSetupNotice,
  runsByWatch,
  watchNotificationCounts,
}: {
  listLoading: boolean;
  pendingActions: Readonly<Record<number, WatchActionIntent>>;
  canEdit: boolean;
  canCreateFromCurrentView: boolean;
  onCreateFromCurrentView(): void;
  creating: boolean;
  draft: PendingCreateDraft | null;
  error: string | null;
  eventsByRun: EventsByRun;
  expanded: Record<number, boolean>;
  highlightedEventsByRun: Record<number, number[]>;
  items: WatchInfo[] | null;
  onRefreshIndex(watchId: number, indexId: string): void;
  onRefreshWatches(): void;
  onRun(watchId: number): void;
  onToggleRuns(watchId: number): void;
  onCancelCreate(): void;
  onCreate(): void;
  onUpdateCreate(patch: Pick<PendingCreateDraft, 'name'>): void;
  onUpdateWatch(watchId: number, patch: { name?: string; enabled?: boolean }): Promise<boolean>;
  onDeleteWatch(watchId: number, focusAfterDeleteId: number | null): Promise<boolean>;
  mutationErrors: Record<number, string>;
  focusWatchId: number | null;
  onWatchFocused(): void;
  notificationSetupNotice: string | null;
  onDismissNotificationSetupNotice(): void;
  runsByWatch: RunsByWatch;
  watchNotificationCounts: WatchNotificationCounts;
}) {
  return (
    <div className="content-stack" data-testid="watches-panel" aria-busy={listLoading}>
      {canEdit && draft && (
        <WatchCreateComposer
          creating={creating}
          draft={draft}
          onCancel={onCancelCreate}
          onCreate={onCreate}
          onUpdate={onUpdateCreate}
        />
      )}
      {canEdit && canCreateFromCurrentView && !draft && (
        <Button type="button" className="btn-primary" data-testid="new-watch-from-current-grid" onClick={onCreateFromCurrentView}>
          <WatchIcon size={14} aria-hidden /> Create new watch
        </Button>
      )}
      {error && <PanelError testId="watches-error">{error}</PanelError>}
      {notificationSetupNotice && (
        <div className="panel-notice" data-tone="warning" data-testid="watch-notification-setup-notice" role="status">
          <span>{notificationSetupNotice}</span>
          <button type="button" className="icon-btn" aria-label="Dismiss notification setup guidance" onClick={onDismissNotificationSetupNotice}>×</button>
        </div>
      )}
      {items === null ? (
        <PanelEmpty>loading...</PanelEmpty>
      ) : items.length === 0 ? (
        <PanelEmpty testId="watches-empty">No watches yet.</PanelEmpty>
      ) : (
        <ul className="panel-list">
          {items.map((watch, index) => (
            <WatchItem
              key={watch.id}
              pendingAction={pendingActions[watch.id]}
              canEdit={canEdit}
              eventsByRun={eventsByRun}
              expanded={Boolean(expanded[watch.id])}
              highlightedEventsByRun={highlightedEventsByRun}
              notificationCount={watchNotificationCounts[watch.id] ?? 0}
              page={runsByWatch[watch.id]}
              watch={watch}
              onRefreshIndex={onRefreshIndex}
              onRefreshWatches={onRefreshWatches}
              onRun={onRun}
              onToggleRuns={onToggleRuns}
              onUpdateWatch={onUpdateWatch}
              onDeleteWatch={onDeleteWatch}
              focusAfterDeleteId={items[index + 1]?.id ?? items[index - 1]?.id ?? null}
              shouldFocus={focusWatchId === watch.id}
              onFocused={onWatchFocused}
              mutationError={mutationErrors[watch.id]}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

export function WatchesPanel({
  canEdit,
  canCreateFromCurrentView = false,
  onCreateFromCurrentView = () => {},
}: {
  canEdit: boolean;
  canCreateFromCurrentView?: boolean;
  onCreateFromCurrentView?(): void;
}) {
  const panel = useWatchesPanelController(canEdit);
  const panelRef = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if (!panel.focusPanel) return;
    panelRef.current?.focus();
    panel.clearFocusPanel();
  }, [panel]);
  return (
    <section ref={panelRef} className="content-stack" data-testid="watches" tabIndex={-1}>
      <span className="sr-only" role="status" aria-live="polite">{panel.status}</span>
      <WatchesPanelHeader
        itemCount={panel.items?.length ?? null}
        notificationTotal={panel.watchNotificationTotal}
      />
      <WatchesPanelBody
        listLoading={panel.listLoading}
        pendingActions={panel.pendingActions}
        canEdit={canEdit}
        canCreateFromCurrentView={canCreateFromCurrentView}
        onCreateFromCurrentView={onCreateFromCurrentView}
        creating={panel.creating}
        draft={panel.pendingCreateDraft}
        error={panel.error}
        eventsByRun={panel.eventsByRun}
        expanded={panel.expanded}
        highlightedEventsByRun={panel.highlightedEventsByRun}
        items={panel.items}
        runsByWatch={panel.runsByWatch}
        watchNotificationCounts={panel.watchNotificationCounts}
        onRefreshIndex={panel.refreshWatchIndex}
        onRefreshWatches={panel.load}
        onRun={panel.runWatch}
        onToggleRuns={panel.toggleRuns}
        onCancelCreate={panel.cancelCreateWatch}
        onCreate={panel.createWatch}
        onUpdateCreate={panel.updateCreateDraft}
        onUpdateWatch={panel.updateWatch}
        onDeleteWatch={panel.deleteWatch}
        mutationErrors={panel.mutationErrors}
        focusWatchId={panel.focusWatchId}
        onWatchFocused={panel.clearFocusWatch}
        notificationSetupNotice={panel.notificationSetupNotice}
        onDismissNotificationSetupNotice={panel.dismissNotificationSetupNotice}
      />
    </section>
  );
}
