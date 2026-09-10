import type { DirectWatchInput, WatchInfo } from '../api/types';
import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';

/** Per-project, memory-only Watch handoffs. Conditional Discover panels can
 * disappear, so in-flight intent must not live in a component or persist. */
export interface WatchRunLink { watchId: number; runId: number; eventIds: number[]; }
export interface PendingWatchRunLink { link: WatchRunLink; target: 'watches'; }

export interface PendingCreateDraft {
  name: string;
  query: DirectWatchInput['query'];
  scope: DirectWatchInput['scope'];
}

export type WatchActionIntent = 'run' | 'refresh_run' | 'rename' | 'pause' | 'resume' | 'delete';
export interface WatchActionOutcome {
  status: string | null;
  error: string | null;
  errorCode: string | null;
  /** A returned Watch upserts locally; null means a successful delete. */
  watch: WatchInfo | null;
  focusWatchId: number | null;
}

const actionRevision = Symbol('watchActionRevision');
export interface WatchActionRecord {
  readonly watchId: number;
  readonly intent: WatchActionIntent;
  readonly phase: 'pending' | 'settled';
  readonly outcome?: WatchActionOutcome;
  readonly [actionRevision]: number;
}

const createRevision = Symbol('createWatchRevision');
export interface CreateWatchRecord {
  readonly draft: PendingCreateDraft;
  readonly phase: 'pending' | 'settled';
  readonly outcome?: WatchActionOutcome;
  readonly [createRevision]: number;
}

export interface WatchRunLinkState {
  pendingWatchRunLink: PendingWatchRunLink | null;
  pendingCreateDraft: PendingCreateDraft | null;
  pendingCreateAction: CreateWatchRecord | null;
  /** One record owns each row continuously from request through ack. */
  watchActions: Readonly<Record<number, WatchActionRecord>>;
}

export function createWatchRunLinkState(): WatchRunLinkState {
  return { pendingWatchRunLink: null, pendingCreateDraft: null, pendingCreateAction: null, watchActions: {} };
}

export function createWatchRunLinkStore(): {
  store: Store<WatchRunLinkState>;
  setPendingWatchRunLink(link: WatchRunLink, target: 'watches'): void;
  ackPendingWatchRunLink(): void;
  setPendingCreateDraft(draft: PendingCreateDraft): void;
  updatePendingCreateDraft(patch: Pick<PendingCreateDraft, 'name'>): void;
  clearPendingCreateDraft(): void;
  claimCreateWatch(): CreateWatchRecord | null;
  settleCreateWatch(record: CreateWatchRecord, outcome: WatchActionOutcome): void;
  ackCreateWatch(record: CreateWatchRecord): void;
  claimWatchAction(watchId: number, intent: WatchActionIntent): WatchActionRecord | null;
  settleWatchAction(record: WatchActionRecord, outcome: WatchActionOutcome): void;
  ackWatchAction(record: WatchActionRecord): void;
} {
  const store = createStore<WatchRunLinkState>(createWatchRunLinkState());
  let nextRevision = 0;
  let draftRevision = 0;
  const sameAction = (current: WatchActionRecord | undefined, record: WatchActionRecord) =>
    current?.[actionRevision] === record[actionRevision];

  return {
    store,
    setPendingWatchRunLink(link, target) { store.set((s) => ({ ...s, pendingWatchRunLink: { link, target } })); },
    ackPendingWatchRunLink() { store.set((s) => s.pendingWatchRunLink ? { ...s, pendingWatchRunLink: null } : s); },
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
    claimCreateWatch() {
      const draft = store.get().pendingCreateDraft;
      if (!draft || store.get().pendingCreateAction) return null;
      const record: CreateWatchRecord = { draft, phase: 'pending', [createRevision]: draftRevision };
      store.set((s) => ({ ...s, pendingCreateAction: record }));
      return record;
    },
    settleCreateWatch(record, outcome) {
      store.set((s) => s.pendingCreateAction?.[createRevision] !== record[createRevision]
        ? s : { ...s, pendingCreateAction: { ...record, phase: 'settled', outcome } });
    },
    ackCreateWatch(record) {
      store.set((s) => {
        if (s.pendingCreateAction?.[createRevision] !== record[createRevision]) return s;
        const clearDraft = record.outcome?.error === null && draftRevision === record[createRevision];
        return { ...s, pendingCreateAction: null, pendingCreateDraft: clearDraft ? null : s.pendingCreateDraft };
      });
    },
    claimWatchAction(watchId, intent) {
      if (!Number.isInteger(watchId) || watchId <= 0 || store.get().watchActions[watchId]) return null;
      const record: WatchActionRecord = { watchId, intent, phase: 'pending', [actionRevision]: ++nextRevision };
      store.set((s) => ({ ...s, watchActions: { ...s.watchActions, [watchId]: record } }));
      return record;
    },
    settleWatchAction(record, outcome) {
      store.set((s) => !sameAction(s.watchActions[record.watchId], record) ? s : {
        ...s,
        watchActions: { ...s.watchActions, [record.watchId]: { ...record, phase: 'settled', outcome } },
      });
    },
    ackWatchAction(record) {
      store.set((s) => {
        if (!sameAction(s.watchActions[record.watchId], record)) return s;
        const watchActions = { ...s.watchActions };
        delete watchActions[record.watchId];
        return { ...s, watchActions };
      });
    },
  };
}

export type WatchRunLinkStoreHandle = ReturnType<typeof createWatchRunLinkStore>;
