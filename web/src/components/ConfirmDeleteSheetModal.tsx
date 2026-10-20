import { ConfirmDialog } from './Modal';
import { Trash2 } from 'lucide-react';
import { PanelError } from './PanelPrimitives';

export interface ConfirmDeleteSheetModalProps {
  sheetName: string;
  dependentSheetNames: string[];
  busy: boolean;
  error: string | null;
  onConfirm(): void;
  onCancel(): void;
}

export function ConfirmDeleteSheetModal({
  sheetName, dependentSheetNames, busy, error, onConfirm, onCancel,
}: ConfirmDeleteSheetModalProps) {
  const blocked = dependentSheetNames.length > 0;
  return <ConfirmDialog title={`Delete ${sheetName}?`} danger busy={busy} blocked={blocked}
    confirmLabel={<><Trash2 size={14} aria-hidden />{busy ? 'Deleting…' : 'Delete sheet'}</>} cancelLabel={blocked ? 'Close' : 'Cancel'}
    onConfirm={onConfirm} onCancel={onCancel} testId="delete-sheet-modal" confirmTestId="delete-sheet-confirm">
    <p className="modal-body-text">
      {blocked
        ? `Delete the dependent ${dependentSheetNames.length === 1 ? 'sheet' : 'sheets'} ${dependentSheetNames.join(', ')} first.`
        : 'This permanently removes the sheet, its rows, columns, and unreferenced file metadata from this project.'}
    </p>
    {error && <PanelError>{error}</PanelError>}
  </ConfirmDialog>;
}
