import { useEffect, useRef } from 'react';
import { AlertTriangle } from 'lucide-react';

export interface ConfirmDeleteSheetModalProps {
  sheetName: string;
  dependentSheetNames: string[];
  busy: boolean;
  error: string | null;
  onConfirm(): void;
  onCancel(): void;
}

export function ConfirmDeleteSheetModal({
  sheetName,
  dependentSheetNames,
  busy,
  error,
  onConfirm,
  onCancel,
}: ConfirmDeleteSheetModalProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const blocked = dependentSheetNames.length > 0;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="modal-card"
      data-testid="delete-sheet-modal"
      aria-labelledby="delete-sheet-modal-title"
      aria-describedby="delete-sheet-modal-description"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!blocked && !busy) onConfirm();
        }}
      >
        <div className="modal-title" id="delete-sheet-modal-title">
          <AlertTriangle size={15} className="modal-warn-icon" /> Delete {sheetName}?
        </div>
        <p className="modal-body-text" id="delete-sheet-modal-description">
          {blocked
            ? `Delete the dependent ${dependentSheetNames.length === 1 ? 'sheet' : 'sheets'} ${dependentSheetNames.join(', ')} first.`
            : 'This permanently removes the sheet, its rows, columns, and unreferenced file metadata from this project.'}
        </p>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="form-actions">
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            {blocked ? 'Close' : 'Cancel'}
          </button>
          {!blocked && (
            <button
              type="submit"
              className="btn btn-reject"
              data-testid="delete-sheet-confirm"
              disabled={busy}
            >
              {busy ? 'Deleting…' : 'Delete sheet'}
            </button>
          )}
        </div>
      </form>
    </dialog>
  );
}
