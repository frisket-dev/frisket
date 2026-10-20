import type { AskEvent } from '../../api/projectQA';

export interface AskSkillProposal {
  event: AskEvent;
  proposalId: string;
  targetSkillId: string | null;
  baseRevision: number | null;
  name: string;
  description: string;
  content: string;
  enabled: boolean;
  scope: 'workspace' | 'organization';
  changes: string | null;
}

export interface AskSkillSaved {
  event: AskEvent;
  attemptId: string;
  proposalId: string;
  skillId: string;
  name: string;
  description: string;
  revision: number;
  currentRevision: number | null;
  enabled: boolean;
  status: 'saved' | 'changed' | 'deleted';
}

export interface AskSkillSaveAttempt {
  event: AskEvent;
  attemptId: string;
  proposalId: string;
  requestId: string;
  content: string;
  name: string;
  description: string;
  enabled: boolean;
  expectedRevision: number | null;
}

export function askSkillProposal(event: AskEvent): AskSkillProposal | null {
  if (event.kind !== 'skill_proposal') return null;
  const value = event.payload;
  if (typeof value.proposal_id !== 'string' || typeof value.name !== 'string'
    || typeof value.description !== 'string' || typeof value.content !== 'string'
    || typeof value.enabled !== 'boolean' || (value.scope !== 'workspace' && value.scope !== 'organization')) return null;
  if (value.target_skill_id !== null && value.target_skill_id !== undefined && typeof value.target_skill_id !== 'string') return null;
  if (value.base_revision !== null && value.base_revision !== undefined && typeof value.base_revision !== 'number') return null;
  return {
    event,
    proposalId: value.proposal_id,
    targetSkillId: typeof value.target_skill_id === 'string' ? value.target_skill_id : null,
    baseRevision: typeof value.base_revision === 'number' ? value.base_revision : null,
    name: value.name,
    description: value.description,
    content: value.content,
    enabled: value.enabled,
    scope: value.scope,
    changes: typeof value.changes === 'string' && value.changes.trim() ? value.changes : null,
  };
}

export function askSkillSaved(event: AskEvent): AskSkillSaved | null {
  if (event.kind !== 'skill_saved') return null;
  const value = event.payload;
  if (typeof value.attempt_id !== 'string' || typeof value.proposal_id !== 'string'
    || typeof value.skill_id !== 'string' || typeof value.name !== 'string'
    || typeof value.description !== 'string' || typeof value.revision !== 'number'
    || typeof value.enabled !== 'boolean') return null;
  const status = value.status === 'changed' || value.status === 'deleted' ? value.status : 'saved';
  return {
    event,
    attemptId: value.attempt_id,
    proposalId: value.proposal_id,
    skillId: value.skill_id,
    name: value.name,
    description: value.description,
    revision: value.revision,
    currentRevision: typeof value.current_revision === 'number' ? value.current_revision : null,
    enabled: value.enabled,
    status,
  };
}

export function askSkillSaveAttempt(event: AskEvent): AskSkillSaveAttempt | null {
  if (event.kind !== 'skill_save_attempt') return null;
  const value = event.payload;
  if (typeof value.attempt_id !== 'string' || typeof value.proposal_id !== 'string'
    || typeof value.request_id !== 'string' || typeof value.content !== 'string'
    || typeof value.name !== 'string' || typeof value.description !== 'string' || typeof value.enabled !== 'boolean') return null;
  if (value.expected_revision !== null && value.expected_revision !== undefined && typeof value.expected_revision !== 'number') return null;
  return {
    event,
    attemptId: value.attempt_id,
    proposalId: value.proposal_id,
    requestId: value.request_id,
    content: value.content,
    name: value.name,
    description: value.description,
    enabled: value.enabled,
    expectedRevision: typeof value.expected_revision === 'number' ? value.expected_revision : null,
  };
}

export interface ResolvedAskSkillProposal {
  proposal: AskSkillProposal;
  saved: AskSkillSaved | null;
  pendingAttempt: AskSkillSaveAttempt | null;
}

/** Reconstruct one editor from its proposal and durable save lifecycle events. */
export function resolveAskSkillProposal(proposal: AskSkillProposal, events: readonly AskEvent[]): ResolvedAskSkillProposal {
  let latestAttempt: AskSkillSaveAttempt | null = null;
  let latestSaved: AskSkillSaved | null = null;
  for (const event of events) {
    const attempt = askSkillSaveAttempt(event);
    if (attempt?.proposalId === proposal.proposalId && (!latestAttempt || attempt.event.seq > latestAttempt.event.seq)) latestAttempt = attempt;
    const saved = askSkillSaved(event);
    if (saved?.proposalId === proposal.proposalId && (!latestSaved || saved.event.seq > latestSaved.event.seq)) latestSaved = saved;
  }
  const pendingAttempt = latestAttempt && latestSaved?.attemptId !== latestAttempt.attemptId ? latestAttempt : null;
  const saved = pendingAttempt ? null : latestSaved;
  const sourceAttempt = pendingAttempt ?? (saved ? latestAttempt : null);
  return {
    proposal: sourceAttempt ? {
      ...proposal,
      content: sourceAttempt.content,
      baseRevision: pendingAttempt?.expectedRevision ?? saved?.revision ?? proposal.baseRevision,
      name: saved?.name ?? sourceAttempt.name,
      description: saved?.description ?? sourceAttempt.description,
      enabled: saved?.enabled ?? sourceAttempt.enabled,
    } : proposal,
    saved,
    pendingAttempt,
  };
}

export function skillProposalTarget(proposal: AskSkillProposal): string {
  return proposal.targetSkillId ?? `new:${proposal.name}`;
}
