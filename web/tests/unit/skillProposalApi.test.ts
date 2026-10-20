import { afterEach, expect, it, vi } from 'vitest';
import { createProjectQAApi } from '../../src/api/projectQA';
import { SkillProposalSaveError } from '../../src/api/skillProposals';

afterEach(() => vi.restoreAllMocks());

it('sends the typed skill proposal save request to the exact Ask proposal', async () => {
  const skill = { id: 'skill', name: 'review', description: 'Review', content: '# Review', enabled: true, revision: 4, createdAt: '', updatedAt: '' };
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
    status: 'saved', attempt_id: 'attempt', proposal_id: 'proposal', skill,
  })));
  const api = createProjectQAApi(() => 'project / 1', () => new Error('unexpected generic error'));
  const result = await api.saveSkillProposal('thread', 'turn', 'proposal', {
    request_id: 'request', content: '# Review', expected_revision: 3,
  });
  expect(result.skill).toEqual(skill);
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/projects/project%20%2F%201/qa/threads/thread/turns/turn/skill-proposals/proposal/save',
    expect.objectContaining({ method: 'POST', body: JSON.stringify({ request_id: 'request', content: '# Review', expected_revision: 3 }) }),
  );
});

it('exposes the typed current revision from a save conflict', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
    detail: { code: 'revision_conflict', message: 'Changed in Settings.', current_revision: 6 },
  }), { status: 409 }));
  const api = createProjectQAApi(() => 'project', () => new Error('unexpected generic error'));
  await expect(api.saveSkillProposal('thread', 'turn', 'proposal', {
    request_id: 'request', content: '# Review', expected_revision: 3,
  })).rejects.toMatchObject<Partial<SkillProposalSaveError>>({ code: 'revision_conflict', currentRevision: 6, message: 'Changed in Settings.' });
});
