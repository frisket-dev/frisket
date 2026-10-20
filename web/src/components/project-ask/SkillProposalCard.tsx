import type { AskSkillProposal, AskSkillSaved } from './skillProposal';

export function SkillProposalCard({ proposal, saved, pendingSave, superseded, onReview }: {
  proposal: AskSkillProposal;
  saved: AskSkillSaved | null;
  pendingSave: boolean;
  superseded: boolean;
  onReview(proposal: AskSkillProposal): void;
}) {
  const status = saved?.status === 'deleted'
    ? 'The saved skill was later deleted.'
    : saved?.status === 'changed'
      ? 'The installed skill has changed since this version was saved.'
      : pendingSave
        ? 'Save interrupted. Review the text to retry.'
        : saved
        ? saved.enabled
          ? `Saved · revision ${saved.revision}. You can use this skill in your next message.`
          : `Saved · revision ${saved.revision}. Enable it in Settings before using it.`
        : superseded
          ? 'A newer draft is available in this conversation.'
          : proposal.targetSkillId
            ? `Update draft · revision ${proposal.baseRevision ?? 'unknown'}`
            : 'New skill draft';
  return <section className={`ask-skill-proposal${superseded ? ' is-superseded' : ''}`} aria-label={`Skill draft: ${proposal.name}`}>
    <div className="ask-skill-proposal-copy"><strong>{proposal.name}</strong><span>{proposal.description}</span><small>{status}</small></div>
    <button type="button" className="btn" onClick={() => onReview(proposal)}>{superseded ? 'Review latest' : 'Review the text'}</button>
  </section>;
}
