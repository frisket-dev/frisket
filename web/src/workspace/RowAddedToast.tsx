import './RowAddedToast.css';
import { useEffect } from 'react';
import { useMainViewModel } from './WorkspaceContext';

const TOAST_MS = 5000;

/** `Row N added · Undo`, bottom-centre of the sheet, gone after five seconds. */
export function RowAddedToastView({
  addedRow,
  onDismiss,
  onUndo,
}: {
  addedRow: { rowNumber: number; changeId: string | null } | null;
  onDismiss(): void;
  onUndo(): void;
}) {
  useEffect(() => {
    if (!addedRow) return undefined;
    const timer = window.setTimeout(onDismiss, TOAST_MS);
    return () => window.clearTimeout(timer);
  }, [addedRow, onDismiss]);
  if (!addedRow) return null;
  return (
    <div className="row-added-toast" role="status" aria-live="polite" data-testid="row-added-toast">
      <span>Row {addedRow.rowNumber.toLocaleString()} added</span>
      {addedRow.changeId && (
        <button type="button" className="row-added-toast-undo" data-testid="row-added-undo" onClick={onUndo}>
          Undo
        </button>
      )}
    </div>
  );
}

export function RowAddedToast() {
  const { addedRow, dismissAddedRow, undoAddedRow } = useMainViewModel();
  return <RowAddedToastView addedRow={addedRow} onDismiss={dismissAddedRow} onUndo={undoAddedRow} />;
}
