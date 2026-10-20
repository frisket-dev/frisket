import type { HttpAskSkillSaveRequest, HttpAskSkillSaveResult } from '../generated/openHttpContracts';

export type SkillProposalSaveRequest = HttpAskSkillSaveRequest & { expected_revision: number | null };

export interface SavedInstructionSkill {
  id: string;
  name: string;
  description: string;
  content: string;
  enabled: boolean;
  revision: number;
  createdAt: string;
  updatedAt: string;
}

export interface SavedSkillProposalResult {
  status: 'saved';
  attempt_id: string;
  proposal_id: string;
  skill: SavedInstructionSkill;
}

export interface ChangedSkillProposalResult {
  status: 'changed';
  attempt_id: string;
  proposal_id: string;
  skill: SavedInstructionSkill;
}

export interface DeletedSkillProposalResult {
  status: 'deleted';
  attempt_id: string;
  proposal_id: string;
  skill: null;
}

export type SkillProposalSaveResult = SavedSkillProposalResult | ChangedSkillProposalResult | DeletedSkillProposalResult;

export type SkillProposalSaveErrorCode =
  | 'revision_conflict'
  | 'name_conflict'
  | 'request_conflict'
  | 'invalid_content'
  | 'forbidden'
  | 'not_found'
  | 'unknown';

export class SkillProposalSaveError extends Error {
  readonly status: number;
  readonly code: SkillProposalSaveErrorCode;
  readonly currentRevision: number | null;

  constructor(status: number, code: SkillProposalSaveErrorCode, message: string, currentRevision: number | null = null) {
    super(message);
    this.name = 'SkillProposalSaveError';
    this.status = status;
    this.code = code;
    this.currentRevision = currentRevision;
  }
}

export function skillProposalSaveRequestError(status: number, payload: unknown): SkillProposalSaveError {
  const detail = payload && typeof payload === 'object' && 'detail' in payload
    ? (payload as { detail?: unknown }).detail
    : payload;
  const record = detail && typeof detail === 'object' ? detail as Record<string, unknown> : null;
  const code = typeof record?.code === 'string' ? record.code as SkillProposalSaveErrorCode
    : status === 403 ? 'forbidden'
      : status === 404 ? 'not_found'
        : status === 422 ? 'invalid_content'
          : 'unknown';
  const message = typeof record?.message === 'string' && record.message.trim()
    ? record.message
    : typeof detail === 'string' && detail.trim()
      ? detail
      : status === 403
        ? 'You no longer have permission to manage skills.'
        : 'Could not save this skill.';
  return new SkillProposalSaveError(
    status,
    code,
    message,
    typeof record?.current_revision === 'number' ? record.current_revision : null,
  );
}

export function mapSkillProposalSaveResult(payload: HttpAskSkillSaveResult): SkillProposalSaveResult {
  const skill = payload.skill;
  if (payload.status === 'deleted') {
    if (skill !== null) throw new Error('The server returned an invalid skill save response.');
    return { ...payload, status: 'deleted', skill: null };
  }
  if (payload.status === 'saved' || payload.status === 'changed') {
    if (skill === null) throw new Error('The server returned an invalid skill save response.');
    return { ...payload, status: payload.status, skill };
  }
  throw new Error('The server returned an invalid skill save response.');
}
