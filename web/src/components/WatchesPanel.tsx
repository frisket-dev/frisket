import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from 'react';
import { PanelEmpty, PanelError } from './PanelPrimitives';
import { ChevronDown, ChevronRight, Play, RefreshCw, Watch } from 'lucide-react';
import {
  ApiError,
  type NotificationSummary,
  type WatchInfo,
  type WatchRun,
  type WatchRunEvent,
  type WatchRunsPage,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { NOTIFICATIONS_CHANGED_EVENT } from './NotificationsPanel';
import { useWatchRunLinkHandle } from '../bind/useWatchRunLinkHandle';
import { useSelector } from '../bind/useSelector';
import type {
  PendingCreateDraft,
  WatchActionOutcome,
  WatchActionIntent,
  WatchRunLink,
} from '../state/watchRunLinkStore';
import { useEditionModule } from '../editions/module';

type RunsByWatch = Record<number, WatchRunsPage>;
type EventsByRun = Record<number, WatchRunEvent[]>;
type WatchNotificationCounts = Record<number, number>;
interface WatchesPanelState {
  items: WatchInfo[] | null;
  runsByWatch: RunsByWatch;
  eventsByRun: EventsByRun;
  highlightedEventsByRun: Record<number, number[]>;
  watchNotificationCounts: WatchNotificationCounts;
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
  watchNotificationCounts: {},
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
  const { projectApi } = useWorkspaceStores();
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
    watchNotificationCounts,
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

  const applyNotificationSummary = useCallback((summary: NotificationSummary) => {
    const counts: WatchNotificationCounts = {};
    for (const item of summary.bySourceRef) {
      if (item.sourceKind !== 'watch') continue;
      const watchId = Number(item.sourceRef.watch_id);
      if (!Number.isInteger(watchId) || watchId <= 0) continue;
      counts[watchId] = item.unseen;
    }
    setPanelState({ watchNotificationCounts: counts });
  }, []);

  const loadNotificationSummary = useCallback(() => {
    void projectApi
      .getNotificationsSummary()
      .then(applyNotificationSummary)
      .catch(() => undefined);
  }, [applyNotificationSummary, projectApi]);

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
    loadNotificationSummary();
  }, [loadNotificationSummary]);

  useEffect(() => {
    const onChanged = () => load();
    window.addEventListener('frisket:watches-changed', onChanged);
    return () => window.removeEventListener('frisket:watches-changed', onChanged);
  }, [load]);

  useEffect(() => {
    const onNotificationsChanged = () => loadNotificationSummary();
    window.addEventListener(NOTIFICATIONS_CHANGED_EVENT, onNotificationsChanged);
    return () => window.removeEventListener(NOTIFICATIONS_CHANGED_EVENT, onNotificationsChanged);
  }, [loadNotificationSummary]);

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
        loadNotificationSummary();
        window.dispatchEvent(new CustomEvent(NOTIFICATIONS_CHANGED_EVENT));
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
  }, [canEdit, expanded, invalidateWatchList, loadNotificationSummary, loadRuns, projectApi, watchRunLinkHandle]);

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
        loadNotificationSummary();
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
  }, [canEdit, invalidateWatchList, loadNotificationSummary, projectApi, watchRunLinkHandle]);

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
        loadNotificationSummary();
        window.dispatchEvent(new CustomEvent(NOTIFICATIONS_CHANGED_EVENT));
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
  }, [canEdit, expanded, invalidateWatchList, loadNotificationSummary, loadRuns, projectApi, watchRunLinkHandle]);

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
// ALLOWLISTED, not folded into
// PanelHeader. It has no title/kicker and no close button — just two
// conditional count badges, and it can render null entirely — so it does not
// fit PanelHeader's kicker/title/chips/actions/onClose shape. It is a
// different genus (a summary strip), not a variant of the header anatomy the
// other six sites share.
function WatchesPanelHeader({
  itemCount,
  notificationTotal,
}: {
  itemCount: number | null;
  notificationTotal: number;
}) {
  if (notificationTotal <= 0 && !(itemCount !== null && itemCount > 0)) return null;
  return (
    <div className="watches-header-slim" data-testid="watches-header">
      {notificationTotal > 0 && (
        <span className="watches-alert-count" data-testid="watches-notification-count">
          {notificationTotal}
        </span>
      )}
      {itemCount !== null && itemCount > 0 && <span className="watches-count">{itemCount}</span>}
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
    <div className="watches-create" data-testid="watch-create-composer">
      <div className="watches-create-heading">
        <span className="panel-kicker">New Watch</span>
        <strong>Keep an eye on new matches.</strong>
      </div>
      <label className="watches-create-field">
        <span className="form-label">Watch name</span>
        <input
          autoFocus
          className="form-input"
          data-testid="watch-create-name"
          aria-label="Watch name"
          aria-describedby={trimmedName ? undefined : 'watch-create-name-error'}
          value={draft.name}
          disabled={creating}
          onChange={(event) => onUpdate({ name: event.target.value })}
        />
      </label>
      {!trimmedName && <span id="watch-create-name-error" data-testid="watch-create-name-error">Enter a name to continue.</span>}
      <div className="watches-create-notifications">
        <p>{NOTIFICATION_SETUP_GUIDANCE}</p>
      </div>
      <div className="form-actions">
        <button
          type="button"
          className="btn btn-primary"
          data-testid="create-watch-button"
          aria-busy={creating}
          disabled={creating || !trimmedName}
          onClick={onCreate}
        >
          <WatchIcon size={14} aria-hidden /> {creating ? 'Creating…' : 'Create new watch'}
        </button>
        <button
          type="button"
          className="btn"
          data-testid="cancel"
          disabled={creating}
          onClick={onCancel}
        >
          Cancel
        </button>
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
    <ul className="watch-run-events" data-testid="watch-run-events">
      {events.map((event) => (
        <li
          className={highlightedEventIds.includes(event.id) ? 'highlighted' : ''}
          key={event.id}
        >
          <span>{event.eventKind}</span>
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
    <ul className="watch-runs" data-testid="watch-run-history">
      {!page || page.runs.length === 0 ? (
        <li className="panel-empty">No runs yet.</li>
      ) : (
        page.runs.map((run) => (
          <li className="watch-run" key={run.id}>
            <span className={`watch-run-status status-${run.status}`}>{run.status}</span>
            <span className="watch-run-rows">
              {run.status === 'error'
                ? (run.errorCode ?? run.error ?? 'Run failed')
                : `${run.matchedRows.toLocaleString()} matched · ${run.newRows.toLocaleString()} new`}
            </span>
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

function ConfirmDeleteWatchDialog({
  busy,
  error,
  id,
  name,
  onCancel,
  onConfirm,
}: {
  busy: boolean;
  error: string | undefined;
  id: number;
  name: string;
  onCancel(): void;
  onConfirm(): void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleId = `delete-watch-title-${id}`;
  const descriptionId = `${titleId}-description`;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="modal-card"
      data-testid="delete-watch-confirmation"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      aria-busy={busy}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!busy) onConfirm();
        }}
      >
        <div className="modal-title" id={titleId}>Delete {name}?</div>
        <p className="modal-body-text" id={descriptionId}>
          Deleting this Watch means its run history and in-app notifications will be deleted; already-sent email or external notifications cannot be recalled.
        </p>
        {error && <PanelError testId="watch-mutation-error">{error}</PanelError>}
        <div className="form-actions">
          <button autoFocus type="button" className="btn" disabled={busy} onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn btn-reject" data-testid="confirm-delete-watch" disabled={busy}>
            {busy ? 'Deleting…' : 'Delete Watch'}
          </button>
        </div>
      </form>
    </dialog>
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
    <li ref={itemRef} className="watch-item" data-testid="watch-item" tabIndex={-1}>
      <div className="watch-head">
        <span className="watch-name">{watch.name}</span>
        {notificationCount > 0 && (
          <span className="watch-notification-count" data-testid="watch-notification-count">
            {notificationCount}
          </span>
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
      <div className="watch-query">
        {watchSummary(watch)}
      </div>
      {!watch.enabled && <span className="watch-status" data-testid="watch-status">Paused</span>}
      <div className="watch-meta" data-testid="watch-latest-run">
        {runSummary(watch.latestRun)}
        {canEdit && blockedIndexId && (
          <button
            type="button"
            className="mini-btn watch-refresh-index"
            data-testid={`watch-refresh-index-${watch.id}`}
            // Ruling 4 honesty: this refreshes the embedding index AND then
            // runs the whole watch again — two purchases, named up front.
            title="Refresh the embedding index, then run the whole watch again"
            aria-busy={isRefreshingIndex}
            disabled={isPending}
            onClick={() => onRefreshIndex(watch.id, blockedIndexId)}
          >
            <RefreshCw size={11} className={isRefreshingIndex ? 'spin' : undefined} /> {isRefreshingIndex ? 'Refreshing index…' : 'Refresh index & re-run'}
          </button>
        )}
      </div>
      {canEdit && (
        <div className="watch-lifecycle-controls">
          {pendingActionLabel && (
            <span data-testid="watch-pending-action" role="status">{pendingActionLabel}</span>
          )}
          {renaming ? (
            <>
              <input
                className="form-input"
                data-testid="watch-rename-input"
                aria-label={`Rename ${watch.name}`}
                aria-describedby={trimmedRename ? undefined : `watch-rename-error-${watch.id}`}
                disabled={isPending}
                value={renameValue}
                onChange={(event) => setRenameValue(event.target.value)}
              />
              {!trimmedRename && <span id={`watch-rename-error-${watch.id}`} data-testid="watch-rename-error">Enter a name to continue.</span>}
              <button type="button" className="mini-btn" data-testid="save-watch-rename" aria-busy={isRenaming} disabled={isPending || !trimmedRename} onClick={() => { void onUpdateWatch(watch.id, { name: trimmedRename }).then((saved) => { if (saved) setRenaming(false); }); }}>{isRenaming ? 'Renaming…' : 'Save rename'}</button>
              <button type="button" className="mini-btn" disabled={isPending} onClick={() => setRenaming(false)}>Cancel</button>
            </>
          ) : (
            <button type="button" className="mini-btn" data-testid="rename-watch" disabled={isPending} onClick={() => { setRenameValue(watch.name); setRenaming(true); }}>Rename</button>
          )}
          <button type="button" className="mini-btn" data-testid={watch.enabled ? 'pause-watch' : 'resume-watch'} aria-busy={isPausing || isResuming} disabled={isPending} onClick={() => { void onUpdateWatch(watch.id, { enabled: !watch.enabled }); }}>{isPausing ? 'Pausing…' : isResuming ? 'Resuming…' : watch.enabled ? 'Pause' : 'Resume'}</button>
          <button ref={deleteButtonRef} type="button" className="mini-btn danger" data-testid="delete-watch" disabled={isPending} onClick={() => setConfirmingDelete(true)}>Delete</button>
        </div>
      )}
      {mutationError && !confirmingDelete && <PanelError testId="watch-mutation-error">{mutationError}</PanelError>}
      {confirmingDelete && (
        <ConfirmDeleteWatchDialog
          busy={isDeleting}
          error={mutationError}
          id={watch.id}
          name={watch.name}
          onCancel={closeDeleteDialog}
          onConfirm={confirmDelete}
        />
      )}
      {canEdit && WatchControlSlot && (
        <WatchControlSlot
          projectId={projectId}
          watch={watch}
          refreshWatches={onRefreshWatches}
        />
      )}
      <button
        type="button"
        className="watch-runs-toggle"
        data-testid="toggle-watch-runs"
        aria-expanded={expanded}
        onClick={() => onToggleRuns(watch.id)}
      >
        <RunChevron size={11} /> Runs
      </button>
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
    <div className="watches-body" data-testid="watches-panel" aria-busy={listLoading}>
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
        <button type="button" className="btn btn-primary" data-testid="new-watch-from-current-grid" onClick={onCreateFromCurrentView}>
          <WatchIcon size={14} aria-hidden /> Create new watch
        </button>
      )}
      {error && <PanelError testId="watches-error">{error}</PanelError>}
      {notificationSetupNotice && (
        <div className="watch-notification-setup-notice" data-testid="watch-notification-setup-notice" role="status">
          <span>{notificationSetupNotice}</span>
          <button type="button" className="icon-btn" aria-label="Dismiss notification setup guidance" onClick={onDismissNotificationSetupNotice}>×</button>
        </div>
      )}
      {items === null ? (
        <PanelEmpty>loading...</PanelEmpty>
      ) : items.length === 0 ? (
        <PanelEmpty testId="watches-empty">No watches yet.</PanelEmpty>
      ) : (
        <ul className="watches-list">
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
    <section ref={panelRef} className="sidebar-watchlists" data-testid="watches" tabIndex={-1}>
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
