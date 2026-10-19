import { ConfirmDialog } from './Modal';

export interface ConfirmDeleteRowsModalProps {
  count: number;
  onConfirm(): void;
  onCancel(): void;
}

export function ConfirmDeleteRowsModal({ count, onConfirm, onCancel }: ConfirmDeleteRowsModalProps) {
  const noun = count === 1 ? 'row' : 'rows';
  return <ConfirmDialog title={`Delete ${count} ${noun}?`} danger
    confirmLabel={`Delete ${noun}`} onConfirm={onConfirm} onCancel={onCancel}
    testId="delete-rows-modal" confirmTestId="delete-rows-confirm" cancelTestId="delete-rows-cancel">
    <p className="modal-body-text">
      The selected {noun} will be removed from the sheet. You can restore {count === 1 ? 'it' : 'them'} with undo.
    </p>
  </ConfirmDialog>;
}
