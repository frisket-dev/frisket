import { describe, expect, it, vi } from 'vitest';
import { createProjectQAStore } from '../../src/state/projectQAStore';
import type { AskThread, AskTurn, ProjectQAApi } from '../../src/api/projectQA';

const thread: AskThread = {
  id: 'thread', title: 'Investigation', scope: { kind: 'project' }, model: null,
  web: false, suggest_actions: true, revision: 1, created_by: null,
  created_at: '', updated_at: '',
};
const turn: AskTurn = {
  id: 'turn', thread_id: 'thread', request_id: 'request', question: 'What changed?',
  scope: { kind: 'project' }, model: null, web: false, suggest_actions: true,
  status: 'running', submitted_by: null, started_at: '', finished_at: null,
  usage: null, cost_actual: null, error_summary: null,
};
const page = { events: [], cursor: 0, has_more: false, active_turn: null };
function fixture(): ProjectQAApi {
  return {
    citation: vi.fn(),
    list: vi.fn(async () => []), create: vi.fn(async () => thread),
    detail: vi.fn(async () => ({ thread, active_turn: turn, history: page })),
    update: vi.fn(async () => thread), delete: vi.fn(async () => {}),
    submit: vi.fn(async () => turn), events: vi.fn(async () => page),
    stop: vi.fn(async () => ({ ...turn, status: 'stopping' as const })),
    report: vi.fn(async () => ({ markdown: '' })),
    researchOptions: vi.fn(async () => ({ available: true, budget_usd: '10', skills: [], web_provider: 'DDGS' })),
    resumeResearch: vi.fn(async () => turn),
  };
}

describe('saved Project Ask', () => {
  it('freezes selection and reuses request id after an uncertain network failure', async () => {
    const api = fixture();
    vi.mocked(api.submit).mockRejectedValueOnce(new Error('Connection lost'));
    const handle = createProjectQAStore(api);
    await handle.initialize({ kind: 'sources', sources: [{ kind: 'rows', sheet_id: 2, row_ids: [7, 8] }] });
    handle.setDraft('What changed?');
    await handle.send();
    await handle.send();
    expect(api.create).toHaveBeenCalledTimes(1);
    const calls = vi.mocked(api.submit).mock.calls;
    expect(calls[0][1]).toEqual(calls[1][1]);
    expect(calls[1][1].scope).toEqual({ kind: 'sources', sources: [{ kind: 'rows', sheet_id: 2, row_ids: [7, 8] }] });
    expect(handle.store.get().draft).toBe('');
  });

  it('drains terminal events before ceasing polling', async () => {
    const api = fixture();
    vi.mocked(api.detail).mockResolvedValue({ thread, active_turn: turn, history: page });
    vi.mocked(api.events).mockResolvedValueOnce({ events: [{
      thread_id: 'thread', turn_id: 'turn', seq: 1, kind: 'answer', payload: { text: 'Answer' }, created_at: '',
    }], cursor: 1, has_more: true, active_turn: null }).mockResolvedValueOnce({ events: [{
      thread_id: 'thread', turn_id: 'turn', seq: 2, kind: 'status', payload: { status: 'completed' }, created_at: '',
    }], cursor: 2, has_more: false, active_turn: null });
    const handle = createProjectQAStore(api);
    await handle.open('thread');
    expect(handle.store.get().events.map((event) => event.seq)).toEqual([1, 2]);
    expect(handle.store.get().activeTurn).toBeNull();
  });

  it('discarded project requests cannot repopulate history', async () => {
    const api = fixture();
    let finish!: (value: AskThread[]) => void;
    vi.mocked(api.list).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const handle = createProjectQAStore(api);
    const loading = handle.initialize({ kind: 'project' });
    handle.dispose();
    finish([thread]);
    await loading;
    expect(handle.store.get().threads).toEqual([]);
    expect(api.detail).not.toHaveBeenCalled();
  });
  it('loads older history without replacing the latest answer', async () => {
    const api = fixture();
    const event = (seq: number) => ({ thread_id: 'thread', turn_id: 'turn', seq, kind: 'assistant' as const, payload: { text: String(seq) }, created_at: '' });
    vi.mocked(api.detail).mockResolvedValue({ thread, active_turn: null, history: { ...page, events: [event(3)], cursor: 3, has_more: true } });
    const handle = createProjectQAStore(api);
    await handle.open('thread');
    vi.mocked(api.events).mockResolvedValueOnce({ ...page, events: [event(1), event(2)], cursor: 2 });
    await handle.loadEarlier();
    expect(api.events).toHaveBeenLastCalledWith('thread', { before: 3, limit: 100 }, expect.any(AbortSignal));
    expect(handle.store.get().events.map((item) => item.seq)).toEqual([1, 2, 3]);
    expect(handle.store.get().hasEarlier).toBe(false);
  });

  it('ignores an older refresh after admitting a new turn', async () => {
    const api = fixture();
    const handle = createProjectQAStore(api);
    await handle.open('thread');
    let finish!: (value: typeof page) => void;
    vi.mocked(api.events).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const oldRefresh = handle.refresh();
    handle.setDraft('A new question');
    vi.mocked(api.events).mockResolvedValueOnce({ ...page, active_turn: turn });
    await handle.send();
    finish(page);
    await oldRefresh;
    expect(handle.store.get().activeTurn?.id).toBe(turn.id);
  });

  it('keeps a failed replay question editable and loads its saved answer', async () => {
    const api = fixture();
    const handle = createProjectQAStore(api);
    await handle.initialize({ kind: 'project' });
    handle.setDraft('What changed?');
    vi.mocked(api.submit).mockRejectedValueOnce(new Error('Lost connection'));
    await handle.send();
    vi.mocked(api.submit).mockResolvedValueOnce({ ...turn, status: 'failed', error_summary: 'Provider unavailable' });
    vi.mocked(api.events).mockResolvedValueOnce({ ...page, events: [{ thread_id: 'thread', turn_id: 'turn', seq: 1, kind: 'question', payload: { question: 'What changed?' }, created_at: '' }] });
    await handle.send();
    expect(handle.store.get().draft).toBe('What changed?');
    expect(handle.store.get().events).toHaveLength(1);
    expect(handle.store.get().error).toBe('Provider unavailable');
    expect(handle.store.get().activeTurn).toBeNull();
    await handle.send();
    const requests = vi.mocked(api.submit).mock.calls;
    expect(requests[2][1].request_id).not.toBe(requests[1][1].request_id);
  });

  it('a terminal stop reply cannot leave the composer disabled', async () => {
    const api = fixture();
    vi.mocked(api.events).mockResolvedValueOnce({ ...page, active_turn: turn });
    const handle = createProjectQAStore(api);
    await handle.open('thread');
    vi.mocked(api.stop).mockResolvedValueOnce({ ...turn, status: 'stopped' });
    vi.mocked(api.events).mockRejectedValueOnce(new Error('Network interrupted'));
    await handle.stop();
    expect(handle.store.get().activeTurn).toBeNull();
  });
});

it('persists research preferences and resumes the exact pending approval revision', async () => {
  const api = fixture();
  const pausedTurn: AskTurn = {
    ...turn,
    research: { write_mode: 'ask_each', budget_usd: '5', max_turns: 8, skills: [] },
    research_state: {
      id: 'research', revision: 4, state: 'paused', budget_micros: 5_000_000, reserved_micros: 0,
      settled_micros: 1_000_000, remaining_micros: 4_000_000, currency: 'USD', write_mode: 'ask_each',
      max_turns: 8, turn_count: 2, pending_approval: { id: 'approval-budget', kind: 'budget' },
    },
  };
  vi.mocked(api.submit).mockResolvedValueOnce(pausedTurn);
  vi.mocked(api.events).mockResolvedValue({ ...page, active_turn: pausedTurn });
  vi.mocked(api.resumeResearch).mockResolvedValueOnce({ ...pausedTurn, research_state: { ...pausedTurn.research_state!, revision: 5, state: 'running', pending_approval: null } });
  const handle = createProjectQAStore(api);
  await handle.initialize({ kind: 'project' });
  expect(handle.store.get().researchConfiguration?.web_provider).toBe('DDGS');

  const research = { write_mode: 'ask_each' as const, budget_usd: '5', max_turns: 8, skills: [] };
  handle.setOptions({ research });
  handle.setDraft('Investigate this');
  await handle.send();
  expect(api.create).toHaveBeenCalledWith(expect.objectContaining({ research }), expect.any(AbortSignal));
  expect(api.submit).toHaveBeenCalledWith('thread', expect.objectContaining({ research }), expect.any(AbortSignal));

  await handle.resumeResearch('continue', { budget_usd: '7' });
  expect(api.resumeResearch).toHaveBeenCalledWith('thread', 'turn', {
    expected_revision: 4, approval_id: 'approval-budget', decision: 'continue', budget_usd: '7',
  }, expect.any(AbortSignal));
});

it('keeps ordinary Ask available when research configuration cannot load', async () => {
  const api = fixture();
  vi.mocked(api.researchOptions).mockRejectedValueOnce(new Error('Research is not configured'))
    .mockResolvedValueOnce({ available: false, budget_usd: null, skills: [], web_provider: null });
  const handle = createProjectQAStore(api);
  await handle.initialize({ kind: 'project' });
  expect(handle.store.get().loaded).toBe(true);
  expect(handle.store.get().researchConfiguration).toBeNull();
  expect(handle.store.get().researchConfigurationError).toBe('Couldn’t load Run actions settings.');
  expect(handle.store.get().error).toBeNull();
  await handle.loadResearchConfiguration();
  expect(handle.store.get().researchConfiguration?.available).toBe(false);
  expect(handle.store.get().researchConfigurationError).toBeNull();
});

it('renames and deletes the saved conversation without executing an action', async () => {
  const api = fixture();
  vi.mocked(api.detail).mockResolvedValue({ thread, active_turn: null, history: page });
  vi.mocked(api.update).mockResolvedValue({ ...thread, title: 'Renamed', revision: 2 });
  const handle = createProjectQAStore(api);
  await handle.open(thread.id);
  expect(await handle.rename('Renamed')).toBe(true);
  expect(api.update).toHaveBeenCalledWith(thread.id, { title: 'Renamed', expected_revision: 1 }, expect.any(AbortSignal));
  expect(handle.store.get().thread?.title).toBe('Renamed');
  handle.store.set((s) => ({ ...s, hasMoreThreads: true }));
  expect(await handle.deleteThread()).toBe(true);
  expect(handle.store.get().hasMoreThreads).toBe(true);
  expect(handle.store.get().thread).toBeNull();
  expect(api.submit).not.toHaveBeenCalled();
});

it('new conversations reset optional outside research', () => {
  const handle = createProjectQAStore(fixture());
  handle.setOptions({ web: true, suggestActions: false, research: { write_mode: 'full_access', budget_usd: '5', max_turns: null, skills: null } });
  handle.newThread({ kind: 'project' });
  expect(handle.store.get().web).toBe(false);
  expect(handle.store.get().suggestActions).toBe(true);
  expect(handle.store.get().research).toBeNull();
});


it('follows current selection only for untouched new conversations', async () => {
  const handle = createProjectQAStore(fixture());
  const dispatches = { kind: 'sources' as const, sources: [{ kind: 'sheet' as const, sheet_id: 1 }] };
  const selection = { kind: 'sources' as const, sources: [{ kind: 'rows' as const, sheet_id: 2, row_ids: [7, 8] }] };
  await handle.initialize(dispatches);
  handle.setDraft('A question I am typing');
  handle.syncContextScope(selection);
  expect(handle.store.get().scope).toEqual(selection);
  expect(handle.store.get().draft).toBe('A question I am typing');
  handle.setOptions({ scope: { kind: 'project' } });
  handle.syncContextScope(dispatches);
  expect(handle.store.get().scope).toEqual({ kind: 'project' });
  handle.newThread(selection);
  handle.syncContextScope(dispatches);
  expect(handle.store.get().scope).toEqual(dispatches);
  await handle.open(thread.id);
  handle.syncContextScope(selection);
  expect(handle.store.get().scope).toEqual(thread.scope);
});


it('freezes context before creating a thread and preserves uncertain retry identity', async () => {
  const api = fixture();
  let created!: (thread: AskThread) => void;
  vi.mocked(api.create).mockImplementationOnce(() => new Promise((resolve) => { created = resolve; }));
  vi.mocked(api.submit).mockRejectedValueOnce(new Error('Connection lost'));
  const handle = createProjectQAStore(api);
  const original = { kind: 'sources' as const, sources: [{ kind: 'sheet' as const, sheet_id: 1 }] };
  await handle.initialize(original);
  handle.setDraft('Largest filing?');
  const sending = handle.send();
  handle.syncContextScope({ kind: 'sources', sources: [{ kind: 'sheet', sheet_id: 2 }] });
  created({ ...thread, scope: original });
  await sending;
  expect(handle.store.get().scope).toEqual(original);
  await handle.send();
  const calls = vi.mocked(api.submit).mock.calls;
  expect(calls[0][1]).toEqual(calls[1][1]);
});

it('deleting a conversation returns to the current workspace context', async () => {
  const handle = createProjectQAStore(fixture());
  await handle.open(thread.id);
  const current = { kind: 'sources' as const, sources: [{ kind: 'sheet' as const, sheet_id: 9 }] };
  handle.syncContextScope(current);
  await handle.deleteThread();
  expect(handle.store.get().scope).toEqual(current);
  handle.syncContextScope({ kind: 'project' });
  expect(handle.store.get().scope).toEqual({ kind: 'project' });
});
