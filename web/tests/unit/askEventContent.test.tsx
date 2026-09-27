// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AskCitation, AskEvent } from '../../src/api/projectQA';
import { AskEventContent } from '../../src/components/project-ask/AskEventContent';
import { askActionProposal } from '../../src/components/project-ask/actionProposal';

const mocks = vi.hoisted(() => ({ citation: vi.fn() }));
vi.mock('../../src/bind/useWorkspaceStores', () => ({ useWorkspaceStores: () => ({ qa: { citation: mocks.citation } }) }));

function event(kind: AskEvent['kind'], payload: AskEvent['payload']): AskEvent {
  return { thread_id: 'thread', turn_id: 'turn', seq: 1, created_at: '', kind, payload };
}

function renderEvent(item: AskEvent, onOpenSource = vi.fn()) {
  return { onOpenSource, ...render(<AskEventContent event={item} onOpenSource={onOpenSource} onInspectProposal={vi.fn()} />) };
}

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('Ask event presentation', () => {
  it('keeps the server failure summary and renders only the safe diagnostic fields', () => {
    renderEvent(event('status', { status: 'failed', error_summary: 'Gemini timed out while preparing a response.', diagnostic: { code: 'internal_error', reference: 'turn-7', tool: 'analytics', raw: 'secret' } }));
    expect(screen.getByText('Gemini timed out while preparing a response.')).toBeInTheDocument();
    expect(screen.getByText('Details')).toBeInTheDocument();
    expect(screen.getByText('turn-7')).toBeInTheDocument();
    expect(screen.queryByText('secret')).not.toBeInTheDocument();
  });

  it('renders every result suggestion as one compact row with singular row copy', () => {
    renderEvent(event('result_suggestion', { citation_id: 'result', title: 'Old title', sheet_name: 'Dispatches', total: 1 }));
    expect(screen.getByRole('button', { name: '1 row in Dispatches' })).toHaveClass('ask-analysis-summary');
    expect(screen.queryByText('Old title')).not.toBeInTheDocument();
  });

  it('navigates local citations without repeating a source preview, while showing changed-source context', async () => {
    const local: AskCitation = { id: 'local', label: 'Local source', excerpt: 'Do not repeat this excerpt', message: null, source_kind: 'cell', status: 'current', target: { kind: 'cell', sheet_id: 1, row_id: 2, column_id: 3 } };
    mocks.citation.mockResolvedValueOnce(local);
    const localView = renderEvent(event('answer', { text: 'Answer', citation_ids: ['local'] }));
    fireEvent.click(screen.getByRole('button', { name: '[1] Source 1' }));
    await waitFor(() => expect(localView.onOpenSource).toHaveBeenCalledWith(local));
    expect(screen.queryByText('Do not repeat this excerpt')).not.toBeInTheDocument();
    cleanup();

    const changed: AskCitation = { ...local, id: 'changed', status: 'changed', message: 'This source changed after the answer.' };
    mocks.citation.mockResolvedValueOnce(changed);
    renderEvent(event('answer', { text: 'Answer', citation_ids: ['changed'] }));
    fireEvent.click(screen.getByRole('button', { name: '[1] Source 1' }));
    await waitFor(() => expect(screen.getByText('This source changed after the answer.')).toBeInTheDocument());
    expect(screen.queryByText('Do not repeat this excerpt')).not.toBeInTheDocument();
  });

  it('turns only current-payload citation markers into inline source buttons', async () => {
    const local: AskCitation = { id: 'local', label: 'Local source', excerpt: null, message: null, source_kind: 'cell', status: 'current', target: { kind: 'cell', sheet_id: 1, row_id: 2, column_id: 3 } };
    mocks.citation.mockResolvedValueOnce(local);
    const view = renderEvent(event('answer', { text: 'Read [spoofed](#cite-1), not [2](#cite-2). [Outside](https://example.test)', citation_ids: ['local'] }));
    const citation = screen.getByRole('button', { name: 'Source 1' });
    expect(citation).toHaveTextContent('1');
    fireEvent.click(citation);
    await waitFor(() => expect(view.onOpenSource).toHaveBeenCalledWith(local));
    expect(screen.queryByText('Sources')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '2' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Outside' })).toHaveAttribute('href', 'https://example.test');
  });

  it('opens only validated same-turn action proposals and known catalog actions inline', () => {
    const proposal = event('action_proposal', {
      proposal: { title: 'Prepared note', spec: { action_id: 'map.template', scope: { kind: 'sheet_rows', sheet_id: 1 }, params: { template: { text: 'note' } }, output_names: { rendered: 'note' } } },
    });
    proposal.seq = 7;
    const prepared = askActionProposal(proposal);
    expect(prepared).not.toBeNull();
    const inspect = vi.fn();
    const openAction = vi.fn();
    render(<AskEventContent event={event('answer', { text: '[Prepared](#action-7) then [Transcribe](#action/media.transcribe), not [Made up](#action/unknown).' })}
      actionProposals={[prepared!]} actionCatalog={[{ kind: 'media.transcribe', title: 'Transcribe' } as never]} onInspectProposal={inspect} onOpenAction={openAction} onOpenSource={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Prepared note' }));
    const transcribe = screen.getByRole('button', { name: 'Transcribe' });
    expect(transcribe).toHaveAttribute('title', 'Media tab > Transcribe');
    fireEvent.click(transcribe);
    expect(inspect).toHaveBeenCalledWith('Prepared note', prepared!.spec);
    expect(openAction).toHaveBeenCalledWith('media.transcribe');
    expect(screen.queryByRole('link', { name: 'Made up' })).not.toBeInTheDocument();
  });

  it('leaves malformed or untrusted reserved markers inert', () => {
    const proposal = event('action_proposal', {
      proposal: { title: 'Other turn', spec: { action_id: 'map.template', scope: { kind: 'sheet_rows', sheet_id: 1 }, params: { template: { text: 'note' } }, output_names: { rendered: 'note' } } },
    });
    proposal.turn_id = 'other-turn';
    proposal.seq = 7;
    const inspect = vi.fn();
    render(<AskEventContent event={event('answer', { text: '[Zero](#cite-0) [Other turn](#action-7) [Unknown](#action/unknown) [Ordinary](https://example.test)', citation_ids: ['source'] })}
      actionProposals={[askActionProposal(proposal)!]} onInspectProposal={inspect} onOpenSource={vi.fn()} />);
    expect(screen.getByText('Zero')).toBeVisible();
    expect(screen.getByText('Other turn')).toBeVisible();
    expect(screen.getByText('Unknown')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Other turn' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Ordinary' })).toHaveAttribute('href', 'https://example.test');
    expect(inspect).not.toHaveBeenCalled();
  });
});
