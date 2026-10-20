import { ConfirmDialog } from './Modal';

export interface OutputColumnCollisionModalProps {
  columns: string[];
  message: string;
  onConfirm(): void;
  onCancel(): void;
}

/** Server-reported collision: confirmation resubmits with replace_existing. */
export function OutputColumnCollisionModal({ columns, message, onConfirm, onCancel }: OutputColumnCollisionModalProps) {
  const noun = columns.length === 1 ? 'column' : 'columns';
  return <ConfirmDialog title={`Overwrite existing ${noun}?`}
    confirmLabel={`Overwrite existing ${noun}`} onConfirm={onConfirm} onCancel={onCancel}
    testId="overwrite-collision-modal" confirmTestId="overwrite-collision-confirm" cancelTestId="overwrite-collision-cancel">
    <p className="modal-body-text">
      {columns.length > 0
        ? `This will overwrite the existing "${columns.join(', ')}" ${noun}.`
        : message || 'This will overwrite existing output columns.'}
    </p>
  </ConfirmDialog>;
}
