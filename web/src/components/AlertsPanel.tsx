import { ViewAlertConditions, ViewAlertSchedule } from './ViewAlertConditions';
import type { ViewAlertConditions as Conditions } from '../api/types';
import './Modal.css';
import { Button } from './Button';
import { ListEditorActions } from './ListEditorActions';
import { TextInput } from './FormControls';
import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from 'react';
import { PanelEmpty, PanelError, StatusChip } from './PanelPrimitives';
import { ChevronDown, ChevronRight, Play, RefreshCw, Watch } from 'lucide-react';
import {
  ApiError,
  type AlertInfo,
  type AlertPatchInput,
  type AlertCheck,
  type AlertCheckEvent,
  type AlertCheckResult,
  type AlertChecksPage,
} from '../api/open';
import { useCostConfirmationRetry } from '../hooks/useCostConfirmationRetry';
import { useJobsHandle } from '../bind/useJobsHandle';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { dispatchNotificationsChanged, useNotificationSummary } from './notifications/useNotificationSummary';
import { ConfirmDialog } from './Modal';
import { useAlertCheckLinkHandle } from '../bind/useAlertCheckLinkHandle';
import { useSelector } from '../bind/useSelector';
import type {
  PendingCreateDraft,
  AlertActionOutcome,
  AlertActionIntent,
  AlertCheckLink,
} from '../state/alertCheckLinkStore';
import { useEditionModule } from '../editions/module';
import './AlertsPanel.css';

const DEFAULT_VIEW_ALERTS: Conditions = { kind: 'conditions', entered: true, exited: false, count_changed: false, fields: [] };

type ChecksByAlert = Record<number, AlertChecksPage>;
type EventsByRun = Record<number, AlertCheckEvent[]>;
type AlertNotificationCounts = Record<number, number>;
interface AlertsPanelState {
  items: AlertInfo[] | null;
  checksByAlert: ChecksByAlert;
  eventsByRun: EventsByRun;
  highlightedEventsByRun: Record<number, number[]>;
  expanded: Record<number, boolean>;
  listLoading: boolean;
  creating: boolean;
  error: string | null;
  errorCode: string | null;
  status: string | null;
  focusAlertId: number | null;
  focusPanel: boolean;
  mutationErrors: Record<number, string>;
  notificationSetupNotice: string | null;
}

type AlertsPanelPatch =
  | Partial<AlertsPanelState>
  | ((state: AlertsPanelState) => Partial<AlertsPanelState>);

const initialAlertsPanelState: AlertsPanelState = {
  items: null,
  checksByAlert: {},
  eventsByRun: {},
  highlightedEventsByRun: {},
  expanded: {},
  listLoading: false,
  creating: false,
  error: null,
  errorCode: null,
  status: null,
  focusAlertId: null,
  focusPanel: false,
  mutationErrors: {},
  notificationSetupNotice: null,
};

const NOTIFICATION_SETUP_GUIDANCE = 'You do not have Slack or email notifications set up. Visit settings to set them up.';
const AlertIcon = Watch;

function reduceAlertsPanelState(
  state: AlertsPanelState,
  patch: AlertsPanelPatch,
): AlertsPanelState {
  return { ...state, ...(typeof patch === 'function' ? patch(state) : patch) };
}

function alertSummary(alert: AlertInfo): string {
  const policy = alert.detectionPolicy;
  return [policy.entered && 'New matches', policy.fields.length > 0 && 'Changed values', policy.exited && 'Rows leaving', policy.count_changed && 'Count changes', policy.threshold && 'Threshold crossings'].filter(Boolean).join(' · ') || 'Alerts paused';
}
function blockedEmbeddingIndexId(alert: AlertInfo): string | null {
  const query = alert.latestRun?.resolvedQuery;
  const similarity = query?.similarity;
  if (!similarity || typeof similarity !== 'object') return null;
  const code = alert.latestRun?.errorCode;
  if (code !== 'embedding_index_incomplete' && code !== 'embedding_index_stale') return null;
  const id = Reflect.get(similarity, 'embedding_index_id');
  return typeof id === 'string' ? id : null;
}

function runSummary(run: AlertCheck | null): string {
  if (!run) return 'Never checked';
  if (run.status === 'error') return run.errorCode ?? run.error ?? 'Check failed';
  return `${run.matchedRows.toLocaleString()} matched · ${run.newRows.toLocaleString()} new`;
}

function failedAlertActionOutcome(error: Error): AlertActionOutcome {
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
    alert: null,
    focusAlertId: null,
  };
}

/**
 * Manual alert runner. This intentionally does not know about schedules or
 * notification channels; it only lists promoted queries and records run history.
 */
function useAlertsPanelController(canEdit: boolean) {
  const { projectApi, chromePreferences: { projectId } } = useWorkspaceStores();
  const notificationSummary = useNotificationSummary();
  const alertCheckLinkHandle = useAlertCheckLinkHandle();
  const [panelState, setPanelState] = useReducer(
    reduceAlertsPanelState,
    initialAlertsPanelState,
  );
  const {
    items,
    checksByAlert,
    eventsByRun,
    highlightedEventsByRun,
    expanded,
    listLoading,
    creating,
    error,
    errorCode,
    status,
    focusAlertId,
    mutationErrors,
    notificationSetupNotice,
  } = panelState;
  const alertListGeneration = useRef(0);

  const invalidateAlertList = useCallback(() => {
    alertListGeneration.current += 1;
    setPanelState({ listLoading: false });
  }, []);

  const alertNotificationCounts: AlertNotificationCounts = {};
  for (const item of notificationSummary?.bySourceRef ?? []) {
    if (item.sourceKind !== 'alert') continue;
    const alertId = Number(item.sourceRef.alert_id);
    if (Number.isInteger(alertId) && alertId > 0) alertNotificationCounts[alertId] = item.unseen;
  }

  const load = useCallback(() => {
    const generation = ++alertListGeneration.current;
    setPanelState({
      listLoading: true,
      error: null,
      errorCode: null,
    });
    void projectApi
      .listAlerts()
      .then((alerts) => {
        if (alertListGeneration.current === generation) setPanelState({ items: alerts });
      })
      .catch((e: Error) => {
        if (alertListGeneration.current === generation) setPanelState({ error: e.message });
      })
      .finally(() => {
        if (alertListGeneration.current === generation) {
          setPanelState({ listLoading: false });
        }
      });
  }, [projectApi]);

  const loadRuns = useCallback((alertId: number) => {
    return projectApi
      .getAlertChecks(alertId, 0, 20)
      .then((page) => {
        setPanelState((current) => ({
          checksByAlert: { ...current.checksByAlert, [alertId]: page },
        }));
        return page;
      });
  }, [projectApi]);

  const loadRunEvents = useCallback((
    alertId: number,
    runId: number,
    eventIds: number[] = [],
  ) => {
    return projectApi
      .getAlertCheckEvents(alertId, runId, 0, 50)
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
    window.addEventListener('frisket:alerts-changed', onChanged);
    return () => window.removeEventListener('frisket:alerts-changed', onChanged);
  }, [load]);

  const applyAlertCheckLink = useCallback((link: AlertCheckLink) => {
    const alertId = Number(link.alertId);
    const runId = Number(link.runId);
    if (!Number.isInteger(alertId) || !Number.isInteger(runId)) return;
    const eventIds = Array.isArray(link.eventIds) ? link.eventIds : [];
    setPanelState((current) => ({
      expanded: { ...current.expanded, [alertId]: true },
    }));
    load();
    void loadRuns(alertId)
      .then(() => loadRunEvents(alertId, runId, eventIds))
      .catch((e: Error) => setPanelState({ error: e.message }));
  }, [load, loadRunEvents, loadRuns]);

  // A notification's "open source" click writes
  // alertCheckLinkStore's pendingAlertCheckLink instead of dispatching a live event —
  // the Discover-tab mount may be unmounted at write time and applies the
  // intent on its later mount. Read the store FRESH (alertCheckLinkHandle.store.get(), not
  // the closed-over selector value) so a StrictMode double-fired effect sees
  // the slot already acked and is a no-op the second time.
  // NO blind clear: only the target this panel owns ('alerts') acks.
  const pendingAlertCheckLink = useSelector(
    alertCheckLinkHandle.store,
    (s) => s.pendingAlertCheckLink,
  );
  const pendingCreateDraft = useSelector(
    alertCheckLinkHandle.store,
    (s) => s.pendingCreateDraft,
  );
  const pendingCreateAction = useSelector(
    alertCheckLinkHandle.store,
    (s) => s.pendingCreateAction,
  );
  const alertActions = useSelector(
    alertCheckLinkHandle.store,
    (s) => s.alertActions,
  );
  useEffect(() => {
    const pending = alertCheckLinkHandle.store.get().pendingAlertCheckLink;
    if (!pending || pending.target !== 'alerts') return;
    applyAlertCheckLink(pending.link);
    alertCheckLinkHandle.ackPendingAlertCheckLink();
  }, [pendingAlertCheckLink, alertCheckLinkHandle, applyAlertCheckLink]);
  useEffect(() => {
    for (const action of Object.values(alertCheckLinkHandle.store.get().alertActions)) {
      if (action.phase !== 'settled' || !action.outcome) continue;
      invalidateAlertList();
      setPanelState((current) => {
        const outcome = action.outcome!;
        const lifecycleError = outcome.error !== null && action.intent !== 'run' && action.intent !== 'refresh_run';
        const items = outcome.error !== null ? current.items : outcome.alert === null
          ? (current.items ?? []).filter((alert) => alert.id !== action.alertId)
          : [...(current.items ?? []).filter((alert) => alert.id !== outcome.alert!.id), outcome.alert!];
        return {
          items,
          status: outcome.status,
          error: lifecycleError ? null : outcome.error,
          errorCode: lifecycleError ? null : outcome.errorCode,
          focusAlertId: outcome.focusAlertId,
          focusPanel: action.intent === 'delete'
            && outcome.error === null
            && outcome.focusAlertId === null,
          mutationErrors: lifecycleError
            ? { ...current.mutationErrors, [action.alertId]: outcome.error ?? '' }
            : current.mutationErrors,
        };
      });
      alertCheckLinkHandle.ackAlertAction(action);
    }
  }, [invalidateAlertList, alertActions, alertCheckLinkHandle]);
  useEffect(() => {
    const action = alertCheckLinkHandle.store.get().pendingCreateAction;
    if (!action || action.phase !== 'settled' || !action.outcome) return;
    invalidateAlertList();
    const outcome = action.outcome;
    setPanelState((current) => ({
      items: outcome.error === null && outcome.alert
        ? [...(current.items ?? []).filter((alert) => alert.id !== outcome.alert!.id), outcome.alert]
        : current.items,
      status: outcome.status,
      error: outcome.error,
      errorCode: outcome.errorCode,
      focusAlertId: outcome.focusAlertId,
      creating: false,
      notificationSetupNotice: outcome.error === null && outcome.alert
        ? NOTIFICATION_SETUP_GUIDANCE
        : current.notificationSetupNotice,
    }));
    alertCheckLinkHandle.ackCreateAlert(action);
  }, [invalidateAlertList, pendingCreateAction, alertCheckLinkHandle]);

  const { requestCostConfirmation } = useJobsHandle();
  const runWithCostConfirmation = useCostConfirmationRetry(requestCostConfirmation);

  const runAlertAction = useCallback((alertId: number, intent: 'run' | 'refresh_run', status: string, request: () => Promise<AlertCheckResult | null>) => {
    if (!canEdit) return;
    const claim = alertCheckLinkHandle.claimAlertAction(alertId, intent);
    if (!claim) return;
    invalidateAlertList();
    setPanelState({ error: null, errorCode: null, status });
    let outcome!: AlertActionOutcome;
    void request()
      .then((result) => {
        if (result === null) {
          outcome = {
            status: null,
            error: null,
            errorCode: null,
            alert: null,
            focusAlertId: alertId,
          };
          return;
        }
        outcome = {
          status: `Finished checking ${result.alert.name}.`,
          error: null,
          errorCode: null,
          alert: result.alert,
          focusAlertId: alertId,
        };
        dispatchNotificationsChanged(projectId);
        if (expanded[alertId]) {
          void loadRuns(alertId).catch((e: Error) => setPanelState({ error: e.message }));
        }
      })
      .catch((e: Error) => { outcome = failedAlertActionOutcome(e); })
      .finally(() => {
        // A list GET started while this mutation was in flight must not
        // publish after the direct mutation result above.
        invalidateAlertList();
        alertCheckLinkHandle.settleAlertAction(claim, outcome);
        window.dispatchEvent(new Event('frisket:alerts-changed'));
      });
  }, [canEdit, expanded, invalidateAlertList, projectId, loadRuns, alertCheckLinkHandle]);

  const checkAlert = useCallback((alertId: number) => {
    runAlertAction(alertId, 'run', 'Checking alert…', () => projectApi.checkAlert(alertId));
  }, [projectApi, runAlertAction]);

  const updateAlert = useCallback((
    alertId: number,
    patch: AlertPatchInput,
  ) => {
    if (!canEdit) return Promise.resolve(false);
    const intent: AlertActionIntent = (patch.detection_policy || patch.schedule !== undefined) ? 'configure' : patch.name === undefined
      ? (patch.enabled ? 'resume' : 'pause')
      : 'rename';
    const claim = alertCheckLinkHandle.claimAlertAction(alertId, intent);
    if (!claim) return Promise.resolve(false);
    invalidateAlertList();
    setPanelState((current) => ({
      error: null,
      errorCode: null,
      status: intent === 'configure' ? 'Saving conditions…' : intent === 'rename' ? 'Renaming alert…' : intent === 'pause' ? 'Pausing alert…' : 'Resuming alert…',
      mutationErrors: { ...current.mutationErrors, [alertId]: '' },
    }));
    let outcome!: AlertActionOutcome;
    return projectApi
      .updateAlert(alertId, patch)
      .then((updated) => {
        outcome = {
          status: (patch.detection_policy || patch.schedule !== undefined) ? `Saved alerts for ${updated.name}.` : patch.name === undefined
            ? `${updated.name} is now ${updated.enabled ? 'active' : 'paused'}.`
            : `Renamed alert to ${updated.name}.`,
          error: null,
          errorCode: null,
          alert: updated,
          focusAlertId: alertId,
        };
        return true;
      })
      .catch((e: Error) => {
        outcome = failedAlertActionOutcome(e);
        return false;
      })
      .finally(() => {
        invalidateAlertList();
        alertCheckLinkHandle.settleAlertAction(claim, outcome);
        window.dispatchEvent(new Event('frisket:alerts-changed'));
      });
  }, [canEdit, invalidateAlertList, projectApi, alertCheckLinkHandle]);

  const deleteAlert = useCallback((alertId: number, focusAfterDeleteId: number | null) => {
    if (!canEdit) return Promise.resolve(false);
    const claim = alertCheckLinkHandle.claimAlertAction(alertId, 'delete');
    if (!claim) return Promise.resolve(false);
    invalidateAlertList();
    setPanelState((current) => ({
      error: null,
      errorCode: null,
      status: 'Deleting alert…',
      mutationErrors: { ...current.mutationErrors, [alertId]: '' },
    }));
    let outcome!: AlertActionOutcome;
    return projectApi
      .deleteAlert(alertId)
      .then(() => {
        outcome = {
          status: 'Alert deleted.',
          error: null,
          errorCode: null,
          alert: null,
          focusAlertId: focusAfterDeleteId,
        };
        dispatchNotificationsChanged(projectId);
        return true;
      })
      .catch((e: Error) => {
        outcome = failedAlertActionOutcome(e);
        return false;
      })
      .finally(() => {
        invalidateAlertList();
        alertCheckLinkHandle.settleAlertAction(claim, outcome);
        window.dispatchEvent(new Event('frisket:alerts-changed'));
      });
  }, [canEdit, invalidateAlertList, projectId, projectApi, alertCheckLinkHandle]);

  // Refresh a blocked embedding alert's index, then re-run the alert.
  const refreshAlertIndex = useCallback((alertId: number, indexId: string) => {
    runAlertAction(alertId, 'refresh_run', 'Refreshing index…', async () => {
      const outcome = await runWithCostConfirmation((confirmation) =>
        confirmation === undefined
          ? projectApi.refreshEmbeddingIndex(indexId, 'incremental')
          : projectApi.refreshEmbeddingIndex(indexId, 'incremental', { confirmation }),
      );
      if (outcome.status !== 'completed') return null;
      return projectApi.checkAlert(alertId);
    });
  }, [projectApi, runAlertAction, runWithCostConfirmation]);

  const toggleRuns = useCallback((alertId: number) => {
    const nextOpen = !expanded[alertId];
    setPanelState((current) => ({
      expanded: { ...current.expanded, [alertId]: nextOpen },
    }));
    if (!nextOpen || checksByAlert[alertId]) return;
    setPanelState({ error: null });
    void loadRuns(alertId)
      .catch((e: Error) => setPanelState({ error: e.message }));
  }, [expanded, loadRuns, checksByAlert]);

  const createAlert = useCallback(() => {
    // Validate the current draft before taking the store-owned claim. The
    // claim is synchronous, so a remounted panel or a rapid second click
    // cannot submit this revision twice.
    const draft = alertCheckLinkHandle.store.get().pendingCreateDraft;
    const name = draft?.name.trim();
    if (!canEdit || !draft || !name) return;
    const action = alertCheckLinkHandle.claimCreateAlert();
    if (!action) return;

    invalidateAlertList();
    setPanelState({ creating: true, error: null, errorCode: null, status: 'Creating alert…' });
    void projectApi
      .createAlert({ name, view_id: draft.view_id, detection_policy: draft.detection_policy, schedule: draft.schedule === undefined ? '@hourly' : draft.schedule })
      .then((alert) => {
        alertCheckLinkHandle.settleCreateAlert(action, {
          status: `Created alert ${alert.name}.`, error: null, errorCode: null, alert, focusAlertId: alert.id,
        });
      })
      .catch((e: Error) => {
        alertCheckLinkHandle.settleCreateAlert(action, failedAlertActionOutcome(e));
      });
  }, [canEdit, invalidateAlertList, projectApi, alertCheckLinkHandle]);

  const cancelCreateAlert = useCallback(() => {
    if (!canEdit || alertCheckLinkHandle.store.get().pendingCreateAction?.phase === 'pending') return;
    alertCheckLinkHandle.clearPendingCreateDraft();
  }, [canEdit, alertCheckLinkHandle]);

  const updateCreateDraft = useCallback((
    patch: Partial<Pick<PendingCreateDraft, 'name' | 'detection_policy' | 'schedule'>>,
  ) => {
    if (!canEdit || alertCheckLinkHandle.store.get().pendingCreateAction?.phase === 'pending') return;
    alertCheckLinkHandle.updatePendingCreateDraft(patch);
  }, [canEdit, alertCheckLinkHandle]);

  const alertNotificationTotal = Object.values(alertNotificationCounts).reduce(
    (sum, count) => sum + count,
    0,
  );

  return {
    listLoading,
    cancelCreateAlert,
    createAlert,
    creating: creating || pendingCreateAction?.phase === 'pending',
    error,
    errorCode,
    eventsByRun,
    expanded,
    highlightedEventsByRun,
    items,
    load,
    pendingCreateDraft,
    refreshAlertIndex,
    checkAlert,
    checksByAlert,
    toggleRuns,
    updateCreateDraft,
    alertNotificationCounts,
    alertNotificationTotal,
    mutationErrors,
    pendingActions: Object.fromEntries(Object.entries(alertActions).map(([id, action]) => [id, action.intent])),
    status,
    focusAlertId,
    clearFocusAlert: () => setPanelState({ focusAlertId: null }),
    focusPanel: panelState.focusPanel,
    clearFocusPanel: () => setPanelState({ focusPanel: false }),
    updateAlert,
    deleteAlert,
    notificationSetupNotice,
    dismissNotificationSetupNotice: () => setPanelState({ notificationSetupNotice: null }),
  };
}

// The Discover tab already labels this panel "Alerts" — no second collapse
// level, no toggle button. This slim static row only exists to keep the
// notification/item count badges visible somewhere once their old toggle
// header is gone; it renders nothing when there is nothing to show.
//
function AlertsPanelHeader({
  itemCount,
  notificationTotal,
}: {
  itemCount: number | null;
  notificationTotal: number;
}) {
  if (notificationTotal <= 0 && !(itemCount !== null && itemCount > 0)) return null;
  return (
    <div className="control-row" data-align="end" data-testid="alerts-header">
      {notificationTotal > 0 && (
        <StatusChip tone="info" size="sm" testId="alerts-notification-count">
          {notificationTotal}
        </StatusChip>
      )}
      {itemCount !== null && itemCount > 0 && <StatusChip tone="neutral" size="sm">{itemCount}</StatusChip>}
    </div>
  );
}

function AlertCreateComposer({
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
  onUpdate(patch: Partial<Pick<PendingCreateDraft, 'name' | 'detection_policy' | 'schedule'>>): void;
}) {
  const trimmedName = draft.name.trim();
  return (
    <div className="panel-card" data-testid="alert-create-composer">
      <div>
        <div className="panel-item-meta">View alerts</div>
        <strong className="panel-item-title">Choose what to alert on.</strong>
      </div>
      <label className="form-field">
        <span className="form-label">Alert name</span>
        <TextInput
          autoFocus

          data-testid="alert-create-name"
          aria-label="Alert name"
          aria-describedby={trimmedName ? undefined : 'alert-create-name-error'}
          value={draft.name}
          disabled={creating}
          onChange={(event) => onUpdate({ name: event.target.value })}
        />
      </label>
      {!trimmedName && <span id="alert-create-name-error" data-testid="alert-create-name-error">Enter a name to continue.</span>}
      <ViewAlertSchedule value={draft.schedule === undefined ? '@hourly' : draft.schedule} disabled={creating}
        onChange={schedule => onUpdate({ schedule })} />
      <ViewAlertConditions value={draft.detection_policy ?? DEFAULT_VIEW_ALERTS} sheetId={draft.sheet_id} disabled={creating}
        onChange={(detection_policy) => onUpdate({ detection_policy })} />
      <div className="panel-notice">
        {NOTIFICATION_SETUP_GUIDANCE}
      </div>
      <div className="form-actions">
        <ListEditorActions testId="create-alert-button" cancelTestId="cancel"
          icon={AlertIcon} saveLabel={creating ? 'Creating…' : 'Create alert'}
          busy={creating} saveDisabled={!trimmedName} onSave={onCreate} onCancel={onCancel} />
      </div>
    </div>
  );
}

function AlertCheckEvents({
  events,
  highlightedEventIds,
}: {
  events: AlertCheckEvent[] | undefined;
  highlightedEventIds: number[];
}) {
  if (!events) return null;
  return (
    <ul className="panel-list alert-check-events" data-testid="alert-check-events">
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

function AlertCheckHistory({
  highlightedEventsByRun,
  eventsByRun,
  page,
}: {
  highlightedEventsByRun: Record<number, number[]>;
  eventsByRun: EventsByRun;
  page: AlertChecksPage | undefined;
}) {
  return (
    <ul className="panel-list" data-testid="alert-check-history">
      {!page || page.runs.length === 0 ? (
        <li className="panel-empty">No runs yet.</li>
      ) : (
        page.runs.map((run) => (
          <li className="panel-list" key={run.id}>
            <div className="control-row">
              <StatusChip tone={run.status === 'ok' ? 'success' : run.status === 'error' ? 'error' : 'neutral'} size="sm" uppercase>{run.status}</StatusChip>
              <span className="panel-item-meta panel-item-main">
                {run.status === 'error'
                  ? (run.errorCode ?? run.error ?? 'Check failed')
                  : `${run.matchedRows.toLocaleString()} matched · ${run.newRows.toLocaleString()} new`}
              </span>
            </div>
            <AlertCheckEvents
              events={eventsByRun[run.id]}
              highlightedEventIds={highlightedEventsByRun[run.id] ?? []}
            />
          </li>
        ))
      )}
    </ul>
  );
}

function AlertItem({
  pendingAction,
  canEdit,
  eventsByRun,
  expanded,
  highlightedEventsByRun,
  notificationCount,
  page,
  alert,
  onRefreshIndex,
  onRefreshAlerts,
  onRun,
  onToggleRuns,
  onUpdateAlert,
  onDeleteAlert,
  focusAfterDeleteId,
  shouldFocus,
  onFocused,
  mutationError,
}: {
  pendingAction: AlertActionIntent | undefined;
  canEdit: boolean;
  eventsByRun: EventsByRun;
  expanded: boolean;
  highlightedEventsByRun: Record<number, number[]>;
  notificationCount: number;
  page: AlertChecksPage | undefined;
  alert: AlertInfo;
  onRefreshIndex(alertId: number, indexId: string): void;
  onRefreshAlerts(): void;
  onRun(alertId: number): void;
  onToggleRuns(alertId: number): void;
  onUpdateAlert(alertId: number, patch: AlertPatchInput): Promise<boolean>;
  onDeleteAlert(alertId: number, focusAfterDeleteId: number | null): Promise<boolean>;
  focusAfterDeleteId: number | null;
  shouldFocus: boolean;
  onFocused(): void;
  mutationError: string | undefined;
}) {
  const RunChevron = expanded ? ChevronDown : ChevronRight;
  const blockedIndexId = blockedEmbeddingIndexId(alert);
  const { AlertControlSlot } = useEditionModule();
  const { projectId } = useWorkspaceStores().chromePreferences;
  const itemRef = useRef<HTMLLIElement>(null);
  const deleteButtonRef = useRef<HTMLButtonElement>(null);
  const [renaming, setRenaming] = useState(false);
  const [configuring, setConfiguring] = useState(false);
  const [conditions, setConditions] = useState(alert.detectionPolicy);
  const [schedule, setSchedule] = useState(alert.schedule);
  const [renameValue, setRenameValue] = useState(alert.name);
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
    void onDeleteAlert(alert.id, focusAfterDeleteId);
  };
  return (
    <li ref={itemRef} className="panel-card alert-item" data-testid="alert-item" tabIndex={-1}>
      <div className="control-row">
        <span className="panel-item-title panel-item-main">{alert.name}</span>
        {notificationCount > 0 && (
          <StatusChip tone="info" size="sm" testId="alert-notification-count">
            {notificationCount}
          </StatusChip>
        )}
        {canEdit && (
          <button
            type="button"
            className="icon-btn"
            // An Alert check always performs a complete fresh evaluation.
            title={`Check ${alert.name} now — a full fresh check, not a resume`}
            aria-label={isRunning ? `Checking ${alert.name}…` : `Check ${alert.name} now — a full fresh check, not a resume`}
            aria-busy={isRunning}
            data-testid="check-alert"
            disabled={isPending}
            onClick={() => onRun(alert.id)}
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
        {alertSummary(alert)}
      </div>
      {canEdit && <Button type="button" disabled={isPending} onClick={() => { setConditions(alert.detectionPolicy); setSchedule(alert.schedule); setConfiguring(!configuring); }}>Conditions</Button>}
      {configuring && <div className="form-stack">
        <ViewAlertSchedule value={schedule} onChange={setSchedule} disabled={isPending} />
        <ViewAlertConditions value={conditions} onChange={setConditions} sheetId={alert.sheetId} disabled={isPending} />
        <ListEditorActions busy={isPending} saveLabel="Save conditions" onSave={() => {
          void onUpdateAlert(alert.id, { detection_policy: conditions, schedule }).then((saved) => { if (saved) setConfiguring(false); });
        }} onCancel={() => setConfiguring(false)} />
      </div>}
      {!alert.enabled && <div className="control-row"><StatusChip tone="neutral" size="sm" testId="alert-status">Paused</StatusChip></div>}
      <div className="control-row panel-item-meta" data-testid="alert-latest-run">
        {runSummary(alert.latestRun)}
        {canEdit && blockedIndexId && (
          <Button
            type="button"
            data-testid={`alert-refresh-index-${alert.id}`}
            // Ruling 4 honesty: this refreshes the embedding index AND then
            // runs the whole alert again — two purchases, named up front.
            title="Refresh the embedding index, then check the whole alert again"
            aria-busy={isRefreshingIndex}
            disabled={isPending}
            onClick={() => onRefreshIndex(alert.id, blockedIndexId)}
          >
            <RefreshCw size={11} className={isRefreshingIndex ? 'spin' : undefined} /> {isRefreshingIndex ? 'Refreshing index…' : 'Refresh index & check again'}
          </Button>
        )}
      </div>
      {canEdit && (
        <div className="control-row">
          {pendingActionLabel && (
            <span data-testid="alert-pending-action" role="status">{pendingActionLabel}</span>
          )}
          {renaming ? (
            <>
              <TextInput
                data-testid="alert-rename-input"
                aria-label={`Rename ${alert.name}`}
                aria-describedby={trimmedRename ? undefined : `alert-rename-error-${alert.id}`}
                disabled={isPending}
                value={renameValue}
                onChange={(event) => setRenameValue(event.target.value)}
              />
              {!trimmedRename && <span id={`alert-rename-error-${alert.id}`} data-testid="alert-rename-error">Enter a name to continue.</span>}
              <ListEditorActions testId="save-alert-rename" saveLabel={isRenaming ? 'Renaming…' : 'Save rename'}
                busy={isRenaming} disabled={isPending} saveDisabled={!trimmedRename}
                onSave={() => { void onUpdateAlert(alert.id, { name: trimmedRename }).then((saved) => { if (saved) setRenaming(false); }); }}
                onCancel={() => setRenaming(false)} />
            </>
          ) : (
            <Button type="button" data-testid="rename-alert" disabled={isPending} onClick={() => { setRenameValue(alert.name); setRenaming(true); }}>Rename</Button>
          )}
          <Button type="button" data-testid={alert.enabled ? 'pause-alert' : 'resume-alert'} aria-busy={isPausing || isResuming} disabled={isPending} onClick={() => { void onUpdateAlert(alert.id, { enabled: !alert.enabled }); }}>{isPausing ? 'Pausing…' : isResuming ? 'Resuming…' : alert.enabled ? 'Pause' : 'Resume'}</Button>
          <Button ref={deleteButtonRef} type="button" className="danger" data-testid="delete-alert" disabled={isPending} onClick={() => setConfirmingDelete(true)}>Delete</Button>
        </div>
      )}
      {mutationError && !confirmingDelete && <PanelError testId="alert-mutation-error">{mutationError}</PanelError>}
      {confirmingDelete && (
        <ConfirmDialog title={`Delete ${alert.name}?`} busy={isDeleting} danger
          testId="delete-alert-confirmation" confirmTestId="confirm-delete-alert"
          confirmLabel={isDeleting ? 'Deleting…' : 'Delete alert'}
          onCancel={closeDeleteDialog} onConfirm={confirmDelete}>
          <p className="modal-body-text">
            Deleting this alert means its check history and in-app notifications will be deleted; already-sent email or external notifications cannot be recalled.
          </p>
          {mutationError && <PanelError testId="alert-mutation-error">{mutationError}</PanelError>}
        </ConfirmDialog>
      )}
      {canEdit && AlertControlSlot && (
        <AlertControlSlot
          projectId={projectId}
          alert={alert}
          refreshAlerts={onRefreshAlerts}
        />
      )}
      <div className="control-row panel-item-meta">
        <Button
          type="button"
          variant="text"
          data-testid="toggle-alert-checks"
          aria-expanded={expanded}
          onClick={() => onToggleRuns(alert.id)}
        >
          <RunChevron size={11} /> Checks
        </Button>
      </div>
      {expanded && (
        <AlertCheckHistory
          eventsByRun={eventsByRun}
          highlightedEventsByRun={highlightedEventsByRun}
          page={page}
        />
      )}
    </li>
  );
}

function AlertsPanelBody({
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
  onRefreshAlerts,
  onRun,
  onToggleRuns,
  onCancelCreate,
  onCreate,
  onUpdateCreate,
  onUpdateAlert,
  onDeleteAlert,
  mutationErrors,
  focusAlertId,
  onAlertFocused,
  notificationSetupNotice,
  onDismissNotificationSetupNotice,
  checksByAlert,
  alertNotificationCounts,
}: {
  listLoading: boolean;
  pendingActions: Readonly<Record<number, AlertActionIntent>>;
  canEdit: boolean;
  canCreateFromCurrentView: boolean;
  onCreateFromCurrentView(): void;
  creating: boolean;
  draft: PendingCreateDraft | null;
  error: string | null;
  eventsByRun: EventsByRun;
  expanded: Record<number, boolean>;
  highlightedEventsByRun: Record<number, number[]>;
  items: AlertInfo[] | null;
  onRefreshIndex(alertId: number, indexId: string): void;
  onRefreshAlerts(): void;
  onRun(alertId: number): void;
  onToggleRuns(alertId: number): void;
  onCancelCreate(): void;
  onCreate(): void;
  onUpdateCreate(patch: Partial<Pick<PendingCreateDraft, 'name' | 'detection_policy' | 'schedule'>>): void;
  onUpdateAlert(alertId: number, patch: AlertPatchInput): Promise<boolean>;
  onDeleteAlert(alertId: number, focusAfterDeleteId: number | null): Promise<boolean>;
  mutationErrors: Record<number, string>;
  focusAlertId: number | null;
  onAlertFocused(): void;
  notificationSetupNotice: string | null;
  onDismissNotificationSetupNotice(): void;
  checksByAlert: ChecksByAlert;
  alertNotificationCounts: AlertNotificationCounts;
}) {
  return (
    <div className="content-stack" data-testid="alerts-panel" aria-busy={listLoading}>
      {canEdit && draft && (
        <AlertCreateComposer
          creating={creating}
          draft={draft}
          onCancel={onCancelCreate}
          onCreate={onCreate}
          onUpdate={onUpdateCreate}
        />
      )}
      {canEdit && canCreateFromCurrentView && !draft && (
        <Button type="button" className="btn-primary" data-testid="new-alert-from-current-grid" onClick={onCreateFromCurrentView}>
          <AlertIcon size={14} aria-hidden /> Create new alert
        </Button>
      )}
      {error && <PanelError testId="alerts-error">{error}</PanelError>}
      {notificationSetupNotice && (
        <div className="panel-notice" data-tone="warning" data-testid="alert-notification-setup-notice" role="status">
          <span>{notificationSetupNotice}</span>
          <button type="button" className="icon-btn" aria-label="Dismiss notification setup guidance" onClick={onDismissNotificationSetupNotice}>×</button>
        </div>
      )}
      {items === null ? (
        <PanelEmpty>loading...</PanelEmpty>
      ) : items.length === 0 ? (
        <PanelEmpty testId="alerts-empty">No alerts yet.</PanelEmpty>
      ) : (
        <ul className="panel-list">
          {items.map((alert, index) => (
            <AlertItem
              key={alert.id}
              pendingAction={pendingActions[alert.id]}
              canEdit={canEdit}
              eventsByRun={eventsByRun}
              expanded={Boolean(expanded[alert.id])}
              highlightedEventsByRun={highlightedEventsByRun}
              notificationCount={alertNotificationCounts[alert.id] ?? 0}
              page={checksByAlert[alert.id]}
              alert={alert}
              onRefreshIndex={onRefreshIndex}
              onRefreshAlerts={onRefreshAlerts}
              onRun={onRun}
              onToggleRuns={onToggleRuns}
              onUpdateAlert={onUpdateAlert}
              onDeleteAlert={onDeleteAlert}
              focusAfterDeleteId={items[index + 1]?.id ?? items[index - 1]?.id ?? null}
              shouldFocus={focusAlertId === alert.id}
              onFocused={onAlertFocused}
              mutationError={mutationErrors[alert.id]}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

export function AlertsPanel({
  canEdit,
  canCreateFromCurrentView = false,
  onCreateFromCurrentView = () => {},
}: {
  canEdit: boolean;
  canCreateFromCurrentView?: boolean;
  onCreateFromCurrentView?(): void;
}) {
  const panel = useAlertsPanelController(canEdit);
  const panelRef = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    if (!panel.focusPanel) return;
    panelRef.current?.focus();
    panel.clearFocusPanel();
  }, [panel]);
  return (
    <section ref={panelRef} className="content-stack" data-testid="alerts" tabIndex={-1}>
      <span className="sr-only" role="status" aria-live="polite">{panel.status}</span>
      <AlertsPanelHeader
        itemCount={panel.items?.length ?? null}
        notificationTotal={panel.alertNotificationTotal}
      />
      <AlertsPanelBody
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
        checksByAlert={panel.checksByAlert}
        alertNotificationCounts={panel.alertNotificationCounts}
        onRefreshIndex={panel.refreshAlertIndex}
        onRefreshAlerts={panel.load}
        onRun={panel.checkAlert}
        onToggleRuns={panel.toggleRuns}
        onCancelCreate={panel.cancelCreateAlert}
        onCreate={panel.createAlert}
        onUpdateCreate={panel.updateCreateDraft}
        onUpdateAlert={panel.updateAlert}
        onDeleteAlert={panel.deleteAlert}
        mutationErrors={panel.mutationErrors}
        focusAlertId={panel.focusAlertId}
        onAlertFocused={panel.clearFocusAlert}
        notificationSetupNotice={panel.notificationSetupNotice}
        onDismissNotificationSetupNotice={panel.dismissNotificationSetupNotice}
      />
    </section>
  );
}
