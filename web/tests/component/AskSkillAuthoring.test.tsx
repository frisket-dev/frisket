// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AskEvent } from '../../src/api/projectQA';
import { SkillProposalSaveError, type SkillProposalSaveResult } from '../../src/api/skillProposals';
import { AskEventContent } from '../../src/components/project-ask/AskEventContent';
import { SkillReviewDialog } from '../../src/components/project-ask/SkillReviewDialog';
import { askSkillProposal } from '../../src/components/project-ask/skillProposal';

const mocks = vi.hoisted(() => ({ citation: vi.fn() }));
vi.mock('../../src/bind/useWorkspaceStores', () => ({ useWorkspaceStores: () => ({ qa: { citation: mocks.citation } }) }));

function proposalEvent(content: string): AskEvent {
  return {
    thread_id: 'thread', turn_id: 'turn', seq: 7, created_at: '', kind: 'skill_proposal', payload: {
      proposal_id: 'proposal', target_skill_id: 'skill', base_revision: 2,
      name: 'contract-review', description: 'Review supplier contracts.', content,
      enabled: true, scope: 'organization',
    },
  };
}

function saved(content: string, revision = 3): SkillProposalSaveResult {
  return {
    status: 'saved', attempt_id: 'attempt', proposal_id: 'proposal',
    skill: {
      id: 'skill', name: 'contract-review', description: 'Review supplier contracts.', content,
      enabled: true, revision, createdAt: '', updatedAt: '',
    },
  };
}

function renderDialog(content: string, overrides: Partial<React.ComponentProps<typeof SkillReviewDialog>> = {}) {
  const proposal = askSkillProposal(proposalEvent(content))!;
  const props: React.ComponentProps<typeof SkillReviewDialog> = {
    proposal, saved: null, pendingAttempt: null, canManage: true, scope: 'organization', researchActive: false,
    onClose: vi.fn(), onSave: vi.fn(async (body) => saved(body.content)), ...overrides,
  };
  return { props, ...render(<SkillReviewDialog {...props} />) };
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('Ask skill authoring', () => {
  it('keeps the chat card compact and opens review through an explicit button', () => {
    const content = '# Private full draft marker\nDo the work.';
    const review = vi.fn();
    render(<AskEventContent event={proposalEvent(content)} onReviewSkill={review} onOpenSource={vi.fn()} onInspectProposal={vi.fn()} />);
    expect(screen.getByText('contract-review')).toBeVisible();
    expect(screen.queryByText(/Private full draft marker/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Review the text' }));
    expect(review).toHaveBeenCalledWith(expect.objectContaining({ content }));
  });

  it('reviews and saves a full 128 KiB draft in a roomy portaled dialog without starting work', async () => {
    const prefix = '---\nname: contract-review\ndescription: Review supplier contracts.\n---\n\n';
    const content = prefix + 'x'.repeat(128 * 1024 - prefix.length);
    const onSave = vi.fn(async (body) => saved(body.content));
    renderDialog(content, { onSave });

    const dialog = screen.getByTestId('ask-skill-review-dialog');
    const editor = screen.getByTestId('ask-skill-editor');
    expect(dialog.parentElement).toBe(document.body);
    expect(dialog).toHaveClass('ask-skill-review-dialog');
    expect(editor).toHaveValue(content);
    expect(screen.getByText('Shared with your organization')).toBeVisible();
    expect(screen.getByText('Visible to everyone in this conversation')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onSave).toHaveBeenCalledOnce());
    expect(onSave.mock.calls[0][0].content).toBe(content);
    expect(await screen.findByRole('status')).toHaveTextContent('Saved. You can use this skill in your next message.');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: /use it now/i })).not.toBeInTheDocument();
  });

  it('reuses the request id after an uncertain response and preserves direct edits', async () => {
    const onSave = vi.fn()
      .mockRejectedValueOnce(new Error('Connection lost'))
      .mockImplementationOnce(async (body) => saved(body.content));
    renderDialog('# Original', { onSave });
    const editor = screen.getByTestId('ask-skill-editor');
    fireEvent.change(editor, { target: { value: '# Edited locally' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Connection lost');
    expect(editor).toHaveValue('# Edited locally');

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
    expect(onSave.mock.calls[1][0]).toEqual(onSave.mock.calls[0][0]);
  });

  it('rebases an edited draft after a revision conflict with a new attempt id', async () => {
    const onSave = vi.fn()
      .mockRejectedValueOnce(new SkillProposalSaveError(409, 'revision_conflict', 'The installed skill changed.', 8))
      .mockImplementationOnce(async (body) => saved(body.content, 9));
    renderDialog('# Original', { onSave });
    const editor = screen.getByTestId('ask-skill-editor');
    fireEvent.change(editor, { target: { value: '# My retained edit' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Keep this draft' }));
    expect(editor).toHaveValue('# My retained edit');
    expect(screen.getByRole('status')).toHaveTextContent('Your draft is unchanged');

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
    expect(onSave.mock.calls[0][0].expected_revision).toBe(2);
    expect(onSave.mock.calls[1][0].expected_revision).toBe(8);
    expect(onSave.mock.calls[1][0].request_id).not.toBe(onSave.mock.calls[0][0].request_id);
  });

  it('shows viewers the complete read-only draft and protects unsaved manager edits on close', () => {
    const viewer = renderDialog('# Read me', { canManage: false });
    expect(screen.getByTestId('ask-skill-editor')).toHaveAttribute('readonly');
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
    expect(screen.getByText('Only a skill manager can edit or save this draft.')).toBeVisible();
    viewer.unmount();

    const manager = renderDialog('# Original');
    fireEvent.change(screen.getByTestId('ask-skill-editor'), { target: { value: '# Unsaved' } });
    fireEvent.click(screen.getAllByRole('button', { name: 'Close' })[0]);
    expect(screen.getByRole('alertdialog', { name: 'Discard unsaved changes?' })).toBeVisible();
    expect(manager.props.onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }));
    expect(screen.getByTestId('ask-skill-editor')).toHaveValue('# Unsaved');
  });
});
