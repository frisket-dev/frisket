// Per-project, in-memory compare-tab owner. The closed kind set is deliberate:
// these are the four first-party compare surfaces, not an extensible tab
// registry. Nothing here is persisted or routed.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';

export type CompareKind = 'ocr' | 'transcribe' | 'translate' | 'topic';

export interface CompareTabSession {
  hasData: boolean;
  verdict: string;
}

export interface CompareTabState {
  open: boolean;
  active: boolean;
  session: CompareTabSession;
  closeWarn: boolean;
}

export type CompareViewState = Record<CompareKind, CompareTabState>;

const COMPARE_KINDS = [
  'ocr',
  'transcribe',
  'translate',
  'topic',
] as const satisfies readonly CompareKind[];

function createCompareTabState(): CompareTabState {
  return {
    open: false,
    active: false,
    session: { hasData: false, verdict: '' },
    closeWarn: false,
  };
}

export function createCompareViewState(): CompareViewState {
  return {
    ocr: createCompareTabState(),
    transcribe: createCompareTabState(),
    translate: createCompareTabState(),
    topic: createCompareTabState(),
  };
}

function withActive(
  state: CompareViewState,
  target: CompareKind,
  openTarget: boolean,
): CompareViewState {
  return Object.fromEntries(
    COMPARE_KINDS.map((kind) => [
      kind,
      {
        ...state[kind],
        ...(kind === target && openTarget ? { open: true } : {}),
        active: kind === target,
      },
    ]),
  ) as CompareViewState;
}

export function createCompareViewStore(): {
  store: Store<CompareViewState>;
  open(kind: CompareKind): void;
  focus(kind: CompareKind): void;
  requestClose(kind: CompareKind): void;
  close(kind: CompareKind): void;
  setSession(kind: CompareKind, session: CompareTabSession): void;
  setCloseWarn(kind: CompareKind, open: boolean): void;
  standDown(): void;
} {
  const store = createStore<CompareViewState>(createCompareViewState());

  return {
    store,

    open(kind) {
      store.set((state) => withActive(state, kind, true));
    },
    focus(kind) {
      store.set((state) => withActive(state, kind, false));
    },
    requestClose(kind) {
      store.set((state) => ({
        ...state,
        [kind]: state[kind].session.hasData
          ? { ...state[kind], closeWarn: true }
          : createCompareTabState(),
      }));
    },
    close(kind) {
      store.set((state) => ({ ...state, [kind]: createCompareTabState() }));
    },
    setSession(kind, session) {
      store.set((state) => ({ ...state, [kind]: { ...state[kind], session } }));
    },
    setCloseWarn(kind, open) {
      store.set((state) => ({ ...state, [kind]: { ...state[kind], closeWarn: open } }));
    },
    standDown() {
      store.set((state) =>
        Object.fromEntries(
          COMPARE_KINDS.map((kind) => [kind, { ...state[kind], active: false }]),
        ) as CompareViewState,
      );
    },
  };
}

export type CompareViewStoreHandle = ReturnType<typeof createCompareViewStore>;
