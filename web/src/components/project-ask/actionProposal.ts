import type { AskEvent } from '../../api/projectQA';
import { isGeneratedActionDraft, type GeneratedActionDraft } from '../../api/types';

export interface AskActionProposal {
  event: AskEvent;
  title: string;
  spec: GeneratedActionDraft;
}

/** Only a persisted, server-validated proposal can back an action anchor. */
export function askActionProposal(event: AskEvent): AskActionProposal | null {
  if (event.kind !== 'action_proposal') return null;
  const proposal = event.payload.proposal;
  if (!proposal || typeof proposal !== 'object' || Array.isArray(proposal)) return null;
  const record = proposal as Record<string, unknown>;
  const spec = record.spec;
  if (!spec || typeof spec !== 'object' || Array.isArray(spec)) return null;
  const draft = spec as Record<string, unknown>;
  if (!isGeneratedActionDraft(draft)) return null;
  return { event, title: typeof record.title === 'string' && record.title.trim() ? record.title : 'Suggested action', spec: draft };
}
