// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AskEvent } from '../../src/api/projectQA';
import { ProjectAskDock } from '../../src/components/ProjectAskDock';

vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });

const state = {
  activeTurn: null, busy: false, draft: '', error: null, events: [] as AskEvent[], hasEarlier: false, hasMoreThreads: false,
  model: null, scope: { kind: 'project' as const }, suggestActions: false, web: false, thread: null, threads: [],
};
const qa = {
  store: { kind: 'ask' }, citation: vi.fn(), initialize: vi.fn().mockResolvedValue(undefined), syncContextScope: vi.fn(),
  refresh: vi.fn(), setOptions: vi.fn(), send: vi.fn(), setDraft: vi.fn(), newThread: vi.fn(), open: vi.fn(), loadEarlier: vi.fn(), loadMoreThreads: vi.fn(), stop: vi.fn(),
};

vi.mock('../../src/bind/useWorkspaceStores', () => ({ useWorkspaceStores: () => ({ qa, chromePreferences: { projectId: 'project' } }) }));
vi.mock('../../src/bind/useSelector', () => ({ useSelector: <T,>(store: { kind: string }, select: (snapshot: unknown) => T) => select(store.kind === 'ask' ? state : { status: 'ready', catalog: { actions: [] } }) }));
vi.mock('../../src/bind/useActionCatalogHandle', () => ({ useActionCatalogHandle: () => ({ store: { kind: 'catalog' } }) }));
vi.mock('../../src/hooks/usePoll', () => ({ usePoll: vi.fn() }));
vi.mock('../../src/hooks/useNativePopover', () => ({ useNativePopover: vi.fn() }));
vi.mock('../../src/hooks/useAnchoredPosition', () => ({ useAnchoredPosition: () => null }));

function event(kind: AskEvent['kind'], seq: number, payload: AskEvent['payload']): AskEvent {
  return { thread_id: 'thread', turn_id: 'turn', seq, created_at: '', kind, payload };
}

afterEach(() => { cleanup(); state.events = []; vi.clearAllMocks(); });

describe('ProjectAskDock inline action presentation', () => {
  it('suppresses a separately rendered prepared proposal only when the same turn references it', () => {
    state.events = [
      event('action_proposal', 7, { proposal: { title: 'Prepared note', spec: { action_id: 'map.template', scope: { kind: 'sheet_rows', sheet_id: 1 }, params: { template: { text: 'note' } }, output_names: { rendered: 'note' } } } }),
      event('answer', 8, { text: 'Use [the prepared note](#action-7).' }),
    ];
    render(<ProjectAskDock initialScope={{ kind: 'project' }} sheets={[]} onClose={vi.fn()} onInspectProposal={vi.fn()} onOpenSource={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Prepared note' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Open action' })).not.toBeInTheDocument();
  });
});
