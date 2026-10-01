import { createStore } from '../core/store/createStore';
import type {
  AskEvent, AskResearchConfiguration, AskResearchOptions, AskResearchResume, AskScope, AskThread, AskTurn,
  AskTurnRequest, ProjectQAApi,
} from '../api/projectQA';

interface AskState {
  threads: AskThread[];
  hasMoreThreads: boolean;
  thread: AskThread | null;
  events: AskEvent[];
  hasEarlier: boolean;
  activeTurn: AskTurn | null;
  draft: string;
  scope: AskScope | null;
  scopeFollowsContext: boolean;
  model: string | null;
  web: boolean;
  suggestActions: boolean;
  research: AskResearchOptions | null;
  researchConfiguration: AskResearchConfiguration | null;
  researchConfigurationError: string | null;
  researchConfigurationLoading: boolean;
  busy: boolean;
  loaded: boolean;
  error: string | null;
}

const initial = (): AskState => ({
  threads: [], hasMoreThreads: false, thread: null, events: [], hasEarlier: false, activeTurn: null, draft: '', scope: null,
  scopeFollowsContext: true, model: null, web: false, suggestActions: true, research: null, researchConfiguration: null,
  researchConfigurationError: null, researchConfigurationLoading: false,
  busy: false, loaded: false, error: null,
});

function researchOptions(value: AskThread['research']): AskResearchOptions | null {
  if (!value) return null;
  return {
    write_mode: value.write_mode ?? 'ask_overwrite',
    budget_usd: value.budget_usd ?? null,
    max_turns: value.max_turns ?? null,
    skills: value.skills ?? null,
  };
}

export function createProjectQAStore(api: ProjectQAApi) {
  const store = createStore<AskState>(initial());
  let generation = 0;
  let configurationGeneration = 0;
  let contextScope: AskScope | null = null;
  let controller = new AbortController();
  let pending: { threadId: string; body: AskTurnRequest } | null = null;
  const error = (value: unknown) => value instanceof Error ? value.message : 'Could not load this conversation.';
  const mergeEvents = (events: AskEvent[]) => store.set((s) => ({
    ...s, events: [...new Map([...s.events, ...events].map((event) => [event.seq, event])).values()].sort((a, b) => a.seq - b.seq),
  }));

  async function loadResearchConfiguration() {
    if (store.get().researchConfigurationLoading) return;
    const current = ++configurationGeneration;
    store.set((s) => ({ ...s, researchConfigurationLoading: true, researchConfigurationError: null }));
    try {
      const researchConfiguration = await api.researchOptions();
      if (current === configurationGeneration) store.set((s) => ({ ...s, researchConfiguration, researchConfigurationLoading: false }));
    } catch {
      if (current === configurationGeneration) store.set((s) => ({ ...s, researchConfiguration: null,
        researchConfigurationLoading: false, researchConfigurationError: 'Couldn’t load Run actions settings.' }));
    }
  }

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
        activeTurn: detail.active_turn, scope: detail.thread.scope, scopeFollowsContext: false, model: detail.thread.model ?? null,
        web: detail.thread.web ?? false, suggestActions: detail.thread.suggest_actions ?? true,
        research: researchOptions(detail.thread.research), busy: false,
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
    async loadMoreThreads() {
      const current = generation;
      try {
        const threads = await api.list(controller.signal, store.get().threads.length);
        if (current !== generation) return;
        store.set((s) => ({ ...s, threads: [...new Map([...s.threads, ...threads].map((item) => [item.id, item])).values()], hasMoreThreads: threads.length === 100 }));
      } catch (exc) { if (current === generation) store.set((s) => ({ ...s, error: error(exc) })); }
    },
    citation: api.citation,
    report: api.report,
    async rename(title: string) {
      const thread = store.get().thread;
      if (!thread || !title.trim()) return false;
      const current = generation;
      try {
        const latest = await api.detail(thread.id, controller.signal);
        if (current !== generation) return false;
        const updated = await api.update(thread.id, { title: title.trim(), expected_revision: latest.thread.revision }, controller.signal);
        if (current !== generation) return false;
        store.set((s) => ({ ...s, thread: updated, threads: s.threads.map((item) => item.id === updated.id ? updated : item), error: null }));
        return true;
      } catch (exc) {
        if (current === generation) store.set((s) => ({ ...s, error: error(exc) }));
        return false;
      }
    },
    async deleteThread() {
      const thread = store.get().thread;
      if (!thread || store.get().activeTurn) return false;
      const current = generation;
      try {
        await api.delete(thread.id, controller.signal);
        if (current !== generation) return false;
        generation += 1; controller.abort(); controller = new AbortController(); pending = null;
        store.set((s) => ({ ...initial(), loaded: true, hasMoreThreads: s.hasMoreThreads,
          threads: s.threads.filter((item) => item.id !== thread.id), scope: contextScope ?? s.scope,
          model: s.model, researchConfiguration: s.researchConfiguration,
          researchConfigurationError: s.researchConfigurationError,
          researchConfigurationLoading: s.researchConfigurationLoading }));
        return true;
      } catch (exc) {
        if (current === generation) store.set((s) => ({ ...s, error: error(exc) }));
        return false;
      }
    },
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
      contextScope = scope;
      if (store.get().loaded || store.get().busy) return;
      const current = generation;
      store.set((s) => ({ ...s, scope: s.scope ?? scope, busy: true }));
      try {
        const threads = await api.list(controller.signal);
        if (current !== generation) return;
        store.set((s) => ({ ...s, threads, hasMoreThreads: threads.length === 100, loaded: true, busy: false }));
        if (threads[0]) await open(threads[0].id);
        await loadResearchConfiguration();
      } catch (exc) {
        if (current === generation) store.set((s) => ({ ...s, busy: false, error: error(exc) }));
      }
    },
    loadResearchConfiguration,
    syncContextScope(scope: AskScope) {
      contextScope = scope;
      const state = store.get();
      if (!state.thread && state.scopeFollowsContext && JSON.stringify(state.scope) !== JSON.stringify(scope)) {
        store.set((s) => ({ ...s, scope }));
      }
    },
    setDraft(draft: string) { store.set((s) => ({ ...s, draft })); },
    setOptions(options: Partial<Pick<AskState, 'scope' | 'model' | 'web' | 'suggestActions' | 'research'>>) {
      store.set((s) => ({ ...s, ...options, scopeFollowsContext: options.scope !== undefined ? false : s.scopeFollowsContext }));
    },
    newThread(scope: AskScope) {
      contextScope = scope;
      generation += 1;
      controller.abort();
      controller = new AbortController();
      pending = null;
      store.set((s) => ({ ...s, thread: null, events: [], hasEarlier: false, activeTurn: null, draft: '', scope,
        scopeFollowsContext: true, web: false, suggestActions: true, research: null, error: null, busy: false }));
    },
    async send() {
      const snapshot = store.get();
      if (!snapshot.draft.trim() || !snapshot.scope || snapshot.busy || snapshot.activeTurn) return;
      const current = ++generation;
      controller.abort();
      controller = new AbortController();
      store.set((s) => ({ ...s, busy: true, error: null, scopeFollowsContext: false }));
      const options = { scope: snapshot.scope, model: snapshot.model, web: snapshot.web, suggest_actions: snapshot.suggestActions,
        ...(snapshot.research ? { research: snapshot.research } : {}) };
      try {
        let thread = snapshot.thread;
        if (!thread) {
          const created = await api.create({ ...options, title: snapshot.draft.trim().slice(0, 100) }, controller.signal);
          if (current !== generation) return;
          thread = created;
          store.set((s) => ({ ...s, thread: created, scopeFollowsContext: false, threads: [created, ...s.threads] }));
        }
        const question = snapshot.draft.trim();
        if (!pending || pending.threadId !== thread.id || pending.body.question !== question
          || JSON.stringify({ ...pending.body, request_id: undefined, question: undefined }) !== JSON.stringify(options)) {
          pending = { threadId: thread.id, body: { ...options, question, request_id: crypto.randomUUID() } };
        }
        const turn = await api.submit(thread.id, pending.body, controller.signal);
        if (current !== generation) return;
        pending = null;
        store.set((s) => ({ ...s, activeTurn: turn.status === 'running' || turn.status === 'stopping' ? turn : null, busy: false, draft: ['failed', 'interrupted', 'stopped'].includes(turn.status) ? snapshot.draft : '', error: turn.error_summary }));
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
        if (current === generation) store.set((s) => ({ ...s, activeTurn: turn.status === 'running' || turn.status === 'stopping' ? turn : null }));
        await refresh();
      } catch (exc) {
        if (current === generation) store.set((s) => ({ ...s, error: error(exc) }));
      }
    },
    async resumeResearch(decision: AskResearchResume['decision'], update: Partial<Pick<AskResearchResume, 'budget_usd' | 'max_turns' | 'write_mode'>> = {}) {
      const { thread, activeTurn } = store.get();
      const research = activeTurn?.research_state;
      const approvalId = research?.pending_approval?.id;
      if (!thread || !activeTurn || research?.state !== 'paused' || typeof approvalId !== 'string') return;
      const current = generation;
      store.set((s) => ({ ...s, busy: true, error: null }));
      try {
        const turn = await api.resumeResearch(thread.id, activeTurn.id, {
          expected_revision: research.revision, approval_id: approvalId, decision, ...update,
        }, controller.signal);
        if (current !== generation) return;
        store.set((s) => ({ ...s, activeTurn: turn.status === 'running' || turn.status === 'stopping' ? turn : null, busy: false }));
        await refresh();
      } catch (exc) {
        if (current === generation) store.set((s) => ({ ...s, busy: false, error: error(exc) }));
      }
    },
    dispose() {
      generation += 1;
      configurationGeneration += 1;
      controller.abort();
      controller = new AbortController();
      store.set((s) => ({ ...s, busy: false, loaded: false, researchConfigurationLoading: false }));
    },
  };
}

export type ProjectQAStoreHandle = ReturnType<typeof createProjectQAStore>;
