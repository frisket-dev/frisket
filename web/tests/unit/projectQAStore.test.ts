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
    vi.mocked(api.detail).mockResolvedValue({ thread, active_turn: null, history: page });
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
});
