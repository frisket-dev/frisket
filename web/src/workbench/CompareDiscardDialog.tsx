import { Button } from '../components/Button';
import { useState } from 'react';
import { Modal } from '../components/Modal';

export function CompareDiscardDialog({ title = 'Discard this scratch session?', description, verdict, testId, onCancel, onDiscard }: {
  title?: string;
  description: string;
  verdict: string;
  testId: string;
  onCancel(): void;
  onDiscard(): void;
}) {
  const [copied, setCopied] = useState(false);
  return <Modal title={title} role="alertdialog" testId={`${testId}-discard-warning`} onClose={onCancel}>
    <p className="modal-body-text">{description} Copy the verdict first if you want to keep it.</p>
    <pre className="compare-discard-verdict">{verdict}</pre>
    <div className="form-actions">
      <Button type="button" data-testid={`${testId}-copy-verdict`} onClick={() => {
        void navigator.clipboard?.writeText(verdict).then(() => setCopied(true)).catch(() => undefined);
      }}>{copied ? 'Copied' : 'Copy verdict'}</Button>
      <Button type="button" data-testid={`${testId}-discard-cancel`} onClick={onCancel}>Cancel</Button>
      <Button type="button" className="btn-reject" data-testid={`${testId}-discard-confirm`} onClick={onDiscard}>Discard</Button>
    </div>
  </Modal>;
}
