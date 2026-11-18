import type { ViewAlertConditions, AlertInfo } from '../api/types';
import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';

/** Per-project, memory-only Alert handoffs. Conditional Discover panels can
 * disappear, so in-flight intent must not live in a component or persist. */
export interface AlertCheckLink { alertId: number; runId: number; eventIds: number[]; }
interface PendingAlertCheckLink { link: AlertCheckLink; target: 'alerts'; }

export interface PendingCreateDraft {
  name: string;
  view_id: number;
  sheet_id?: number | null;
  detection_policy?: ViewAlertConditions;
  schedule?: string | null;
}

export type AlertActionIntent = 'run' | 'refresh_run' | 'configure' | 'rename' | 'pause' | 'resume' | 'delete';
export interface AlertActionOutcome {
  status: string | null;
  error: string | null;
  errorCode: string | null;
  /** A returned Alert upserts locally; null means a successful delete. */
  alert: AlertInfo | null;
  focusAlertId: number | null;
}

const actionRevision = Symbol('alertActionRevision');
interface AlertActionRecord {
  readonly alertId: number;
  readonly intent: AlertActionIntent;
  readonly phase: 'pending' | 'settled';
  readonly outcome?: AlertActionOutcome;
  readonly [actionRevision]: number;
}

const createRevision = Symbol('createAlertRevision');
interface CreateAlertRecord {
  readonly draft: PendingCreateDraft;
  readonly phase: 'pending' | 'settled';
  readonly outcome?: AlertActionOutcome;
  readonly [createRevision]: number;
}

interface AlertCheckLinkState {
  pendingAlertCheckLink: PendingAlertCheckLink | null;
  pendingCreateDraft: PendingCreateDraft | null;
  pendingCreateAction: CreateAlertRecord | null;
  /** One record owns each row continuously from request through ack. */
  alertActions: Readonly<Record<number, AlertActionRecord>>;
}

function createAlertCheckLinkState(): AlertCheckLinkState {
  return { pendingAlertCheckLink: null, pendingCreateDraft: null, pendingCreateAction: null, alertActions: {} };
}

export function createAlertCheckLinkStore(): {
  store: Store<AlertCheckLinkState>;
  setPendingAlertCheckLink(link: AlertCheckLink, target: 'alerts'): void;
  ackPendingAlertCheckLink(): void;
  setPendingCreateDraft(draft: PendingCreateDraft): void;
  updatePendingCreateDraft(patch: Partial<Pick<PendingCreateDraft, 'name' | 'detection_policy'>>): void;
  clearPendingCreateDraft(): void;
  claimCreateAlert(): CreateAlertRecord | null;
  settleCreateAlert(record: CreateAlertRecord, outcome: AlertActionOutcome): void;
  ackCreateAlert(record: CreateAlertRecord): void;
  claimAlertAction(alertId: number, intent: AlertActionIntent): AlertActionRecord | null;
  settleAlertAction(record: AlertActionRecord, outcome: AlertActionOutcome): void;
  ackAlertAction(record: AlertActionRecord): void;
} {
  const store = createStore<AlertCheckLinkState>(createAlertCheckLinkState());
  let nextRevision = 0;
  let draftRevision = 0;
  const sameAction = (current: AlertActionRecord | undefined, record: AlertActionRecord) =>
    current?.[actionRevision] === record[actionRevision];

  return {
    store,
    setPendingAlertCheckLink(link, target) { store.set((s) => ({ ...s, pendingAlertCheckLink: { link, target } })); },
    ackPendingAlertCheckLink() { store.set((s) => s.pendingAlertCheckLink ? { ...s, pendingAlertCheckLink: null } : s); },
    setPendingCreateDraft(draft) { draftRevision = ++nextRevision; store.set((s) => ({ ...s, pendingCreateDraft: draft })); },
    updatePendingCreateDraft(patch) {
      store.set((s) => {
        if (!s.pendingCreateDraft || s.pendingCreateAction?.phase === 'pending') return s;
        draftRevision = ++nextRevision;
        return { ...s, pendingCreateDraft: { ...s.pendingCreateDraft, ...patch } };
      });
    },
    clearPendingCreateDraft() {
      store.set((s) => s.pendingCreateAction?.phase === 'pending' || !s.pendingCreateDraft
        ? s : { ...s, pendingCreateDraft: null });
    },
    claimCreateAlert() {
      const draft = store.get().pendingCreateDraft;
      if (!draft || store.get().pendingCreateAction) return null;
      const record: CreateAlertRecord = { draft, phase: 'pending', [createRevision]: draftRevision };
      store.set((s) => ({ ...s, pendingCreateAction: record }));
      return record;
    },
    settleCreateAlert(record, outcome) {
      store.set((s) => s.pendingCreateAction?.[createRevision] !== record[createRevision]
        ? s : { ...s, pendingCreateAction: { ...record, phase: 'settled', outcome } });
    },
    ackCreateAlert(record) {
      store.set((s) => {
        if (s.pendingCreateAction?.[createRevision] !== record[createRevision]) return s;
        const clearDraft = record.outcome?.error === null && draftRevision === record[createRevision];
        return { ...s, pendingCreateAction: null, pendingCreateDraft: clearDraft ? null : s.pendingCreateDraft };
      });
    },
    claimAlertAction(alertId, intent) {
      if (!Number.isInteger(alertId) || alertId <= 0 || store.get().alertActions[alertId]) return null;
      const record: AlertActionRecord = { alertId, intent, phase: 'pending', [actionRevision]: ++nextRevision };
      store.set((s) => ({ ...s, alertActions: { ...s.alertActions, [alertId]: record } }));
      return record;
    },
    settleAlertAction(record, outcome) {
      store.set((s) => !sameAction(s.alertActions[record.alertId], record) ? s : {
        ...s,
        alertActions: { ...s.alertActions, [record.alertId]: { ...record, phase: 'settled', outcome } },
      });
    },
    ackAlertAction(record) {
      store.set((s) => {
        if (!sameAction(s.alertActions[record.alertId], record)) return s;
        const alertActions = { ...s.alertActions };
        delete alertActions[record.alertId];
        return { ...s, alertActions };
      });
    },
  };
}

export type AlertCheckLinkStoreHandle = ReturnType<typeof createAlertCheckLinkStore>;
