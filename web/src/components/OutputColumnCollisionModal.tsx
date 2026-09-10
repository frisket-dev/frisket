import { useEffect, useRef } from 'react';
import { AlertTriangle } from 'lucide-react';

export interface OutputColumnCollisionModalProps {
  columns: string[];
  message: string;
  onConfirm(): void;
  onCancel(): void;
}

/** The server rejected a launch with
 *  `output_column_exists` (precheck_fn in map_ai.py, sdk/maprunner.py) —
 *  server truth caught a collision the form's own (possibly stale)
 *  `sheet.columns` snapshot missed. Same low-friction affordance ActionPanel
 *  already offers for the client-detected case (a single "Overwrite existing
 *  column" click, no typed confirmation — overwriting a prior AI-generated
 *  column is a re-run, not data loss) and the same single-click
 *  `<dialog>`/`modal-card` shape ConfirmDeleteRowsModal uses. Confirming
 *  resubmits the exact same native request with `replace_existing: true`. */
export function OutputColumnCollisionModal({
  columns,
  message,
  onConfirm,
  onCancel,
}: OutputColumnCollisionModalProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const noun = columns.length === 1 ? 'column' : 'columns';
  const columnList = columns.length ? columns.join(', ') : 'the requested output';

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
      data-testid="overwrite-collision-modal"
      aria-labelledby="overwrite-collision-modal-title"
      aria-describedby="overwrite-collision-modal-description"
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
        <div className="modal-title" id="overwrite-collision-modal-title">
          <AlertTriangle size={15} className="modal-warn-icon" /> Overwrite existing{' '}
          {noun}?
        </div>
        <p className="modal-body-text" id="overwrite-collision-modal-description">
          {/* The server error's own message names the action kind, not the
              specific column(s) (map_ai.py's build_precheck_fn) — the column
              name(s) come from ActionError.details.columns instead (server
              truth, not the client's possibly-stale list), same as the
              client-detected warning's own copy
              (`This will overwrite the existing "X" column.`). */}
          {columns.length > 0
            ? `This will overwrite the existing "${columnList}" ${noun}.`
            : message || 'This will overwrite existing output columns.'}
        </p>
        <div className="form-actions">
          <button
            type="button"
            className="btn"
            onClick={onCancel}
            data-testid="overwrite-collision-cancel"
          >
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            data-testid="overwrite-collision-confirm"
          >
            Overwrite existing {noun}
          </button>
        </div>
      </form>
    </dialog>
  );
}
