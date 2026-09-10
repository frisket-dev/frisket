import { useEffect, useRef } from 'react';
import { AlertTriangle } from 'lucide-react';

export interface ConfirmDeleteRowsModalProps {
  count: number;
  onConfirm(): void;
  onCancel(): void;
}

/** Confirm dialog for the toolbar trash action. Deletes are soft (rows are
 *  hidden, not destroyed) and reversible through undo, so a single confirm
 *  click is enough — no typed phrase. */
export function ConfirmDeleteRowsModal({ count, onConfirm, onCancel }: ConfirmDeleteRowsModalProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const noun = count === 1 ? 'row' : 'rows';

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) {
      dialog.showModal();
    }
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="modal-card"
      data-testid="delete-rows-modal"
      aria-labelledby="delete-rows-modal-title"
      aria-describedby="delete-rows-modal-description"
      onCancel={(e) => {
        e.preventDefault();
        onCancel();
      }}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onConfirm();
        }}
      >
        <div className="modal-title" id="delete-rows-modal-title">
          <AlertTriangle size={15} className="modal-warn-icon" /> Delete {count} {noun}?
        </div>
        <p className="modal-body-text" id="delete-rows-modal-description">
          The selected {noun} will be removed from the sheet. You can restore {count === 1 ? 'it' : 'them'} with undo.
        </p>
        <div className="form-actions">
          <button type="button" className="btn" onClick={onCancel} data-testid="delete-rows-cancel">
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn-reject"
            data-testid="delete-rows-confirm"
          >
            Delete {noun}
          </button>
        </div>
      </form>
    </dialog>
  );
}
