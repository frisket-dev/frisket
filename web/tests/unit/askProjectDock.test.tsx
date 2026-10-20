// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AskEvent, AskResearchConfiguration, AskResearchOptions, AskTurn } from '../../src/api/projectQA';
import { ProjectAskDock } from '../../src/components/ProjectAskDock';

vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });

const state = {
  activeTurn: null as AskTurn | null, busy: false, draft: '', error: null, events: [] as AskEvent[], hasEarlier: false, hasMoreThreads: false,
  model: null, scope: { kind: 'project' as const }, suggestActions: false, web: false, thread: null, threads: [],
  research: null as AskResearchOptions | null, researchConfiguration: null as AskResearchConfiguration | null,
  researchConfigurationError: null as string | null, researchConfigurationLoading: false,
  canManageSkills: false, skillScope: null as 'workspace' | 'organization' | null,
};
const qa = {
  store: { kind: 'ask' }, citation: vi.fn(), initialize: vi.fn().mockResolvedValue(undefined), syncContextScope: vi.fn(),
  refresh: vi.fn(), setOptions: vi.fn(), send: vi.fn(), setDraft: vi.fn(), newThread: vi.fn(), open: vi.fn(), loadEarlier: vi.fn(), loadMoreThreads: vi.fn(), stop: vi.fn(), resumeResearch: vi.fn(), loadResearchConfiguration: vi.fn(),
  saveSkillProposal: vi.fn(),
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

afterEach(() => { cleanup(); state.events = []; state.activeTurn = null; state.research = null; state.researchConfiguration = null; state.researchConfigurationError = null; state.researchConfigurationLoading = false; state.canManageSkills = false; state.skillScope = null; vi.clearAllMocks(); });

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

  it('keeps a prepared proposal visible when its marker is only a code example', () => {
    state.events = [
      event('action_proposal', 7, { proposal: { title: 'Prepared note', spec: { action_id: 'map.template', scope: { kind: 'sheet_rows', sheet_id: 1 }, params: { template: { text: 'note' } }, output_names: { rendered: 'note' } } } }),
      event('answer', 8, { text: 'Example: `[the prepared note](#action-7)`.' }),
    ];
    render(<ProjectAskDock initialScope={{ kind: 'project' }} sheets={[]} onClose={vi.fn()} onInspectProposal={vi.fn()} onOpenSource={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Open action' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Prepared note' })).not.toBeInTheDocument();
  });
});

describe('ProjectAskDock skill drafts', () => {
  it('reconstructs saved state from persisted events and keeps a viewer read-only', () => {
    state.events = [
      event('skill_proposal', 7, { proposal_id: 'proposal', target_skill_id: 'skill', base_revision: 2,
        name: 'contract-review', description: 'Review contracts', content: '# Original proposal', enabled: true, scope: 'organization' }),
      event('skill_save_attempt', 8, { attempt_id: 'attempt', proposal_id: 'proposal', request_id: 'request',
        target_skill_id: 'skill', expected_revision: 2, content: '# Complete edited instructions', name: 'contract-review',
        description: 'Review contracts', enabled: true }),
      event('skill_saved', 9, { attempt_id: 'attempt', proposal_id: 'proposal', skill_id: 'skill', name: 'contract-review',
        description: 'Review contracts', revision: 3, enabled: true, status: 'saved' }),
    ];
    render(<ProjectAskDock initialScope={{ kind: 'project' }} sheets={[]} onClose={vi.fn()} onInspectProposal={vi.fn()} onOpenSource={vi.fn()} />);
    expect(screen.getByText('Saved · revision 3. You can use this skill in your next message.')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Review the text' }));
    expect(screen.getByTestId('ask-skill-editor')).toHaveValue('# Complete edited instructions');
    expect(screen.getByTestId('ask-skill-editor')).toHaveAttribute('readonly');
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
  });

  it('retains the last reviewed text and requires an explicit refresh when the installed skill changed', () => {
    state.canManageSkills = true;
    state.skillScope = 'workspace';
    state.events = [
      event('skill_proposal', 7, { proposal_id: 'proposal', target_skill_id: 'skill', base_revision: 2,
        name: 'contract-review', description: 'Review contracts', content: '# Original', enabled: true, scope: 'workspace' }),
      event('skill_save_attempt', 8, { attempt_id: 'attempt', proposal_id: 'proposal', request_id: 'request',
        target_skill_id: 'skill', expected_revision: 2, content: '# My reviewed text', name: 'contract-review',
        description: 'Review contracts', enabled: true }),
      event('skill_saved', 9, { attempt_id: 'attempt', proposal_id: 'proposal', skill_id: 'skill', name: 'contract-review',
        description: 'Review contracts', revision: 3, current_revision: 6, enabled: true, status: 'changed' }),
    ];
    render(<ProjectAskDock initialScope={{ kind: 'project' }} sheets={[]} onClose={vi.fn()} onInspectProposal={vi.fn()} onOpenSource={vi.fn()} />);
    expect(screen.getByText('The installed skill has changed since this version was saved.')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Review the text' }));
    expect(screen.getByTestId('ask-skill-editor')).toHaveValue('# My reviewed text');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Keep this draft' }));
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
    expect(screen.getByTestId('ask-skill-editor')).toHaveValue('# My reviewed text');
  });

  it('restores an unmatched durable attempt and retries its exact request id', async () => {
    state.canManageSkills = true;
    state.events = [
      event('skill_proposal', 7, { proposal_id: 'proposal', target_skill_id: null, base_revision: null,
        name: 'contract-review', description: 'Review contracts', content: '# Original', enabled: true, scope: 'workspace' }),
      event('skill_save_attempt', 8, { attempt_id: 'attempt', proposal_id: 'proposal', request_id: 'durable-request',
        target_skill_id: null, expected_revision: null, content: '# Unsure if saved', name: 'supplier-review',
        description: 'Review suppliers', enabled: true }),
    ];
    qa.saveSkillProposal.mockResolvedValueOnce({ status: 'saved', attempt_id: 'attempt', proposal_id: 'proposal',
      skill: { id: 'skill', name: 'contract-review', description: 'Review contracts', content: '# Unsure if saved',
        enabled: true, revision: 3, createdAt: '', updatedAt: '' } });
    render(<ProjectAskDock initialScope={{ kind: 'project' }} sheets={[]} onClose={vi.fn()} onInspectProposal={vi.fn()} onOpenSource={vi.fn()} />);
    expect(screen.getByText('Save interrupted. Review the text to retry.')).toBeVisible();
    expect(screen.getByText('supplier-review')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Review the text' }));
    expect(screen.getByTestId('ask-skill-editor')).toHaveValue('# Unsure if saved');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await vi.waitFor(() => expect(qa.saveSkillProposal).toHaveBeenCalledWith('turn', 'proposal', {
      request_id: 'durable-request', content: '# Unsure if saved', expected_revision: null,
    }));
  });
});

describe('ProjectAskDock research controls', () => {
  it('enables Run actions with the live provider, budget, and canonical skills', async () => {
    state.researchConfiguration = {
      available: true, budget_usd: '12', web_provider: 'Exa',
      skills: [{ name: 'document-research', description: 'Research documents' }],
    };
    render(<ProjectAskDock initialScope={{ kind: 'project' }} sheets={[]} onClose={vi.fn()} onInspectProposal={vi.fn()} onOpenSource={vi.fn()} />);

    await userEvent.click(screen.getByRole('button', { name: 'Ask options' }));
    await userEvent.click(screen.getByLabelText('Run actions'));
    expect(qa.setOptions).toHaveBeenCalledWith({
      research: { write_mode: 'ask_overwrite', budget_usd: null, max_turns: null, skills: null },
    });
  });

  it('explains unavailable research and retries a failed configuration request', async () => {
    state.researchConfiguration = { available: false, reason: 'Project editor access is required.', budget_usd: null, web_provider: null, skills: [] };
    const { rerender } = render(<ProjectAskDock initialScope={{ kind: 'project' }} sheets={[]} onClose={vi.fn()} onInspectProposal={vi.fn()} onOpenSource={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Ask options' }));
    expect(screen.getByText('Project editor access is required.')).toBeInTheDocument();
    expect(screen.getByLabelText('Run actions')).toBeDisabled();

    state.researchConfiguration = null;
    state.researchConfigurationError = 'Couldn’t load Run actions settings.';
    rerender(<ProjectAskDock initialScope={{ kind: 'project' }} sheets={[]} onClose={vi.fn()} onInspectProposal={vi.fn()} onOpenSource={vi.fn()} />);
    const researchSettings = screen.getByLabelText('Research settings');
    expect(within(researchSettings).getByText('Couldn’t load Run actions settings.')).toBeInTheDocument();
    fireEvent.click(within(researchSettings).getByText('Retry'));
    expect(qa.loadResearchConfiguration).toHaveBeenCalledOnce();
  });

  it('approves the exact pending action through the research resume path', async () => {
    state.activeTurn = {
      id: 'turn', thread_id: 'thread', request_id: 'request', question: 'Research', scope: { kind: 'project' },
      model: null, web: false, suggest_actions: true, status: 'running', submitted_by: null, started_at: '',
      finished_at: null, usage: null, cost_actual: null, error_summary: null,
      research_state: {
        id: 'research', revision: 3, state: 'paused', budget_micros: 1_000_000, reserved_micros: 0,
        settled_micros: 0, remaining_micros: 1_000_000, currency: 'USD', write_mode: 'ask_each', max_turns: null,
        turn_count: 1, pending_approval: { id: 'approval', kind: 'action', title: 'Add a column',
          effect: 'Writes one generated column.', targets: ['Sources'], can_skip: true },
      },
    };
    render(<ProjectAskDock initialScope={{ kind: 'project' }} sheets={[]} onClose={vi.fn()} onInspectProposal={vi.fn()} onOpenSource={vi.fn()} />);

    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(qa.resumeResearch).toHaveBeenCalledWith('approve', undefined);
  });
});
