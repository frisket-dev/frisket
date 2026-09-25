import { createStore } from '../core/store/createStore';
import type { AskEvent, AskScope, AskThread, AskTurn, AskTurnRequest, ProjectQAApi } from '../api/projectQA';

interface AskState {
  threads: AskThread[];
  thread: AskThread | null;
  events: AskEvent[];
  hasEarlier: boolean;
  activeTurn: AskTurn | null;
  draft: string;
  scope: AskScope | null;
  model: string | null;
  web: boolean;
  suggestActions: boolean;
  busy: boolean;
  loaded: boolean;
  error: string | null;
}

const initial = (): AskState => ({
  threads: [], thread: null, events: [], hasEarlier: false, activeTurn: null, draft: '', scope: null,
  model: null, web: false, suggestActions: true, busy: false, loaded: false, error: null,
});

export function createProjectQAStore(api: ProjectQAApi) {
  const store = createStore<AskState>(initial());
  let generation = 0;
  let controller = new AbortController();
  let pending: { threadId: string; body: AskTurnRequest } | null = null;
  const error = (value: unknown) => value instanceof Error ? value.message : 'Could not load this conversation.';
  const mergeEvents = (events: AskEvent[]) => store.set((s) => ({
    ...s, events: [...new Map([...s.events, ...events].map((event) => [event.seq, event])).values()].sort((a, b) => a.seq - b.seq),
  }));

  async function open(threadId: string) {
    const current = ++generation;
    controller.abort();
    controller = new AbortController();
    store.set((s) => ({ ...s, busy: true, error: null }));
    try {
      const detail = await api.detail(threadId, controller.signal);
      if (current !== generation) return;
      pending = null;
      store.set((s) => ({ ...s, thread: detail.thread, events: detail.history.events,
        hasEarlier: detail.history.has_more,
        activeTurn: detail.active_turn, scope: detail.thread.scope, model: detail.thread.model ?? null,
        web: detail.thread.web ?? false, suggestActions: detail.thread.suggest_actions ?? true, busy: false,
      }));
      await refresh();
    } catch (exc) {
      if (current === generation) store.set((s) => ({ ...s, busy: false, error: error(exc) }));
    }
  }

  async function refresh() {
    const current = generation;
    const threadId = store.get().thread?.id;
    if (!threadId) return;
    try {
      let after = store.get().events.at(-1)?.seq ?? 0;
      let more = true;
      while (more && current === generation) {
        const page = await api.events(threadId, { after, limit: 200 }, controller.signal);
        if (current !== generation) return;
        mergeEvents(page.events);
        after = page.cursor;
        more = page.has_more;
        if (!more) store.set((s) => ({ ...s, activeTurn: page.active_turn }));
      }
    } catch (exc) {
      if (current === generation) store.set((s) => ({ ...s, error: error(exc) }));
    }
  }

  return {
    store, open, refresh,
    citation: api.citation,
    async loadEarlier() {
      const current = generation;
      const snapshot = store.get();
      if (!snapshot.thread || !snapshot.hasEarlier || snapshot.busy) return;
      store.set((s) => ({ ...s, busy: true }));
      try {
        const page = await api.events(snapshot.thread.id, { before: snapshot.events[0]?.seq, limit: 100 }, controller.signal);
        if (current !== generation) return;
        mergeEvents(page.events);
        store.set((s) => ({ ...s, hasEarlier: page.has_more, busy: false }));
      } catch (exc) {
        if (current === generation) store.set((s) => ({ ...s, busy: false, error: error(exc) }));
      }
    },
    async initialize(scope: AskScope) {
      if (store.get().loaded || store.get().busy) return;
      const current = generation;
      store.set((s) => ({ ...s, scope: s.scope ?? scope, busy: true }));
      try {
        const threads = await api.list(controller.signal);
        if (current !== generation) return;
        store.set((s) => ({ ...s, threads, loaded: true, busy: false }));
        if (threads[0]) await open(threads[0].id);
      } catch (exc) {
        if (current === generation) store.set((s) => ({ ...s, busy: false, error: error(exc) }));
      }
    },
    setDraft(draft: string) { store.set((s) => ({ ...s, draft })); },
    setOptions(options: Partial<Pick<AskState, 'scope' | 'model' | 'web' | 'suggestActions'>>) {
      store.set((s) => ({ ...s, ...options }));
    },
    newThread(scope: AskScope) {
      generation += 1;
      controller.abort();
      controller = new AbortController();
      pending = null;
      store.set((s) => ({ ...s, thread: null, events: [], hasEarlier: false, activeTurn: null, draft: '', scope, error: null, busy: false }));
    },
    async send() {
      const snapshot = store.get();
      if (!snapshot.draft.trim() || !snapshot.scope || snapshot.busy || snapshot.activeTurn) return;
      const current = ++generation;
      controller.abort();
      controller = new AbortController();
      store.set((s) => ({ ...s, busy: true, error: null }));
      const options = { scope: snapshot.scope, model: snapshot.model, web: snapshot.web, suggest_actions: snapshot.suggestActions };
      try {
        let thread = snapshot.thread;
        if (!thread) {
          const created = await api.create({ ...options, title: snapshot.draft.trim().slice(0, 100) }, controller.signal);
          if (current !== generation) return;
          thread = created;
          store.set((s) => ({ ...s, thread: created, threads: [created, ...s.threads] }));
        }
        const question = snapshot.draft.trim();
        if (!pending || pending.threadId !== thread.id || pending.body.question !== question
          || JSON.stringify({ ...pending.body, request_id: undefined, question: undefined }) !== JSON.stringify(options)) {
          pending = { threadId: thread.id, body: { ...options, question, request_id: crypto.randomUUID() } };
        }
        const turn = await api.submit(thread.id, pending.body, controller.signal);
        if (current !== generation) return;
        pending = null;
        store.set((s) => ({ ...s, activeTurn: turn.status === 'running' || turn.status === 'stopping' ? turn : null, busy: false, draft: '' }));
        await refresh();
      } catch (exc) {
        if (current === generation) store.set((s) => ({ ...s, busy: false, error: error(exc) }));
      }
    },
    async stop() {
      const { thread, activeTurn } = store.get();
      if (!thread || !activeTurn) return;
      const current = generation;
      try {
        const turn = await api.stop(thread.id, activeTurn.id, controller.signal);
        if (current === generation) store.set((s) => ({ ...s, activeTurn: turn }));
        await refresh();
      } catch (exc) {
        if (current === generation) store.set((s) => ({ ...s, error: error(exc) }));
      }
    },
    dispose() {
      generation += 1;
      controller.abort();
      controller = new AbortController();
      store.set((s) => ({ ...s, busy: false, loaded: false }));
    },
  };
}

export type ProjectQAStoreHandle = ReturnType<typeof createProjectQAStore>;
