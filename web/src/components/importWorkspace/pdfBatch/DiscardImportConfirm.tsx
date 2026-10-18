import { AlertTriangle } from 'lucide-react';

export interface DiscardImportConfirmProps {
  title: string;
  message: string;
  confirmLabel: string;
  busy?: boolean;
  testId: string;
  onKeep(): void;
  onConfirm(): void;
}

export function DiscardImportConfirm({
  title,
  message,
  confirmLabel,
  busy = false,
  testId,
  onKeep,
  onConfirm,
}: DiscardImportConfirmProps) {
  return (
    <div className="modal-backdrop pdf-batch-discard-dialog" data-testid={testId}>
      <form className="modal-card" onSubmit={(event) => { event.preventDefault(); onConfirm(); }}>
        <div className="modal-title"><AlertTriangle size={15} className="modal-warn-icon" /> {title}</div>
        <p className="modal-body-text">{message}</p>
        <div className="form-actions">
          <button type="button" className="btn" autoFocus disabled={busy} onClick={onKeep}>Keep working</button>
          <button type="submit" className="btn btn-danger" disabled={busy}>{confirmLabel}</button>
        </div>
      </form>
    </div>
  );
}
