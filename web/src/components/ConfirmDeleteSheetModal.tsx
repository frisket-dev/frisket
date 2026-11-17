import { ConfirmDialog } from './Modal';
import { Trash2 } from 'lucide-react';
import { PanelError } from './PanelPrimitives';

export interface ConfirmDeleteSheetModalProps {
  sheetName: string;
  busy: boolean;
  error: string | null;
  onConfirm(): void;
  onCancel(): void;
}

export function ConfirmDeleteSheetModal({
  sheetName, busy, error, onConfirm, onCancel,
}: ConfirmDeleteSheetModalProps) {
  return <ConfirmDialog title={`Delete ${sheetName}?`} danger busy={busy}
    confirmLabel={<><Trash2 size={14} aria-hidden />{busy ? 'Deleting…' : 'Delete sheet'}</>} cancelLabel="Cancel"
    onConfirm={onConfirm} onCancel={onCancel} testId="delete-sheet-modal" confirmTestId="delete-sheet-confirm">
    <p className="modal-body-text">
      Remove this sheet from the project. You can undo this change; other sheets keep their data.
    </p>
    {error && <PanelError>{error}</PanelError>}
  </ConfirmDialog>;
}
