import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Save } from 'lucide-react';
import type { SkillProposalSaveRequest, SkillProposalSaveResult } from '../../api/skillProposals';
import { SkillProposalSaveError } from '../../api/skillProposals';
import type { AskSkillProposal, AskSkillSaveAttempt, AskSkillSaved } from './skillProposal';
import { WindowCloseButton } from '../WindowCloseButton';

export function SkillReviewDialog({ proposal, saved, pendingAttempt, canManage, scope, researchActive, onClose, onSave }: {
  proposal: AskSkillProposal;
  saved: AskSkillSaved | null;
  pendingAttempt: AskSkillSaveAttempt | null;
  canManage: boolean;
  scope: 'workspace' | 'organization';
  researchActive: boolean;
  onClose(): void;
  onSave(body: SkillProposalSaveRequest): Promise<SkillProposalSaveResult>;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const retryRef = useRef<{ requestId: string; content: string; expectedRevision: number | null } | null>(pendingAttempt ? {
    requestId: pendingAttempt.requestId,
    content: pendingAttempt.content,
    expectedRevision: pendingAttempt.expectedRevision,
  } : null);
  const [draft, setDraft] = useState(proposal.content);
  const [savedContent, setSavedContent] = useState(proposal.content);
  const [expectedRevision, setExpectedRevision] = useState(saved?.revision ?? proposal.baseRevision);
  const [busy, setBusy] = useState(false);
  const [permissionLost, setPermissionLost] = useState(false);
  const [hasCurrentSavedRevision, setHasCurrentSavedRevision] = useState(saved?.status === 'saved' && !pendingAttempt);
  const [error, setError] = useState<string | null>(saved?.status === 'deleted'
    ? 'This saved skill was deleted in Settings. The reviewed text remains available here.'
    : saved?.status === 'changed'
      ? 'The installed skill changed after this version was saved. Review your retained text, then keep it for a new revision.'
      : null);
  const [conflictRevision, setConflictRevision] = useState<number | null>(saved?.status === 'changed' ? saved.currentRevision : null);
  const [requiresRefresh, setRequiresRefresh] = useState(saved?.status === 'changed');
  const [confirmation, setConfirmation] = useState<string | null>(pendingAttempt
    ? 'The previous Save did not finish. Save again to retry the same request.'
    : saved?.status === 'saved'
    ? `Saved · revision ${saved.revision}.`
    : null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const dirty = draft !== savedContent;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    return () => { if (dialog.open && typeof dialog.close === 'function') dialog.close(); };
  }, []);

  const requestClose = () => {
    if (busy) return;
    if (dirty) setConfirmDiscard(true);
    else onClose();
  };

  const save = async () => {
    if (busy || !canManage || permissionLost) return;
    const existing = retryRef.current;
    const attempt = existing && existing.content === draft && existing.expectedRevision === expectedRevision
      ? existing
      : { requestId: crypto.randomUUID(), content: draft, expectedRevision };
    retryRef.current = attempt;
    setBusy(true);
    setError(null);
    setConflictRevision(null);
    setConfirmation(null);
    try {
      const result = await onSave({
        request_id: attempt.requestId,
        content: attempt.content,
        expected_revision: attempt.expectedRevision,
      });
      if (result.status === 'deleted') {
        retryRef.current = null;
        setError('This saved skill was deleted in Settings. Your edited text remains here.');
        return;
      }
      if (result.status === 'changed') {
        setConflictRevision(result.skill.revision);
        setRequiresRefresh(true);
        setError('The installed skill changed after this version was saved. Review your retained text, then keep it for a new revision.');
        return;
      }
      setSavedContent(result.skill.content);
      setDraft(result.skill.content);
      setExpectedRevision(result.skill.revision);
      setHasCurrentSavedRevision(true);
      retryRef.current = null;
      setConfirmation(result.skill.enabled
        ? researchActive
          ? 'Saved. You can use the updated skill in your next message.'
          : 'Saved. You can use this skill in your next message.'
        : 'Saved. This skill is disabled; enable it in Settings before using it.');
    } catch (cause) {
      if (cause instanceof SkillProposalSaveError) {
        setError(cause.message);
        if (cause.code === 'revision_conflict' && cause.currentRevision !== null) {
          setConflictRevision(cause.currentRevision);
          setRequiresRefresh(true);
        }
        if (cause.code === 'forbidden') setPermissionLost(true);
      } else {
        setError(cause instanceof Error ? cause.message : 'Could not save this skill.');
      }
    } finally {
      setBusy(false);
    }
  };

  const dialog = <dialog
    ref={dialogRef}
    className="modal-card ask-skill-review-dialog"
    aria-labelledby="ask-skill-review-title"
    data-testid="ask-skill-review-dialog"
    onCancel={(event) => { event.preventDefault(); requestClose(); }}
  >
    <header className="ask-skill-review-header">
      <h2 id="ask-skill-review-title">{proposal.name}</h2>
      <WindowCloseButton label="Close skill review" onClick={requestClose} disabled={busy} />
    </header>
    <div className="ask-skill-review-meta">
      <p>{proposal.description}</p>
      <span>{scope === 'organization' ? 'Shared with your organization' : 'Available across this workspace'}</span>
      <span>Visible to everyone in this conversation</span>
    </div>
    <p className="ask-skill-review-safety">Skills are instructions. Do not include credentials or secrets.</p>
    {proposal.changes && <details className="ask-skill-review-changes"><summary>What changed</summary><pre>{proposal.changes}</pre></details>}
    <label className="ask-skill-review-editor">Complete instructions
      <textarea
        className="form-input"
        data-testid="ask-skill-editor"
        value={draft}
        readOnly={!canManage || permissionLost}
        spellCheck={false}
        onChange={(event) => { setDraft(event.currentTarget.value); if (!requiresRefresh && saved?.status !== 'deleted') setError(null); setConfirmation(null); }}
      />
    </label>
    {!canManage && <p className="ask-skill-review-readonly">Only a skill manager can edit or save this draft.</p>}
    {permissionLost && <p className="ask-skill-review-readonly">Your skill-management permission changed. Your edited text remains here for you to copy.</p>}
    {error && <div className="ask-skill-review-error" role="alert"><p>{error}</p>{conflictRevision !== null && <button type="button" className="btn" onClick={() => {
      setExpectedRevision(conflictRevision);
      retryRef.current = null;
      setConflictRevision(null);
      setRequiresRefresh(false);
      setError(null);
      setConfirmation('Your draft is unchanged. Saving will replace the installed instructions as a new revision.');
    }}>Keep this draft</button>}</div>}
    {confirmation && <p className="ask-skill-review-status" role="status">{confirmation}</p>}
    <footer className="ask-skill-review-footer">
      {canManage && !permissionLost && saved?.status !== 'deleted' && <button type="button" className="btn btn-accept" disabled={busy || requiresRefresh || (hasCurrentSavedRevision && !dirty)} onClick={() => void save()}><Save size={14} />{busy ? 'Saving…' : 'Save'}</button>}
      <button type="button" className="btn" disabled={busy} onClick={requestClose}>Close</button>
    </footer>
    {confirmDiscard && <div className="ask-skill-discard" role="alertdialog" aria-modal="true" aria-labelledby="ask-skill-discard-title">
      <div className="ask-skill-discard-card"><strong id="ask-skill-discard-title">Discard unsaved changes?</strong><p>Your edits have not been saved to the skill library.</p>
        <div><button type="button" className="btn" onClick={() => setConfirmDiscard(false)}>Keep editing</button><button type="button" className="btn btn-reject" onClick={onClose}>Discard and close</button></div>
      </div>
    </div>}
  </dialog>;

  return createPortal(dialog, document.body);
}
