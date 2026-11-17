import { ConfirmDialog } from './Modal';

interface OutputColumnCollisionModalProps {
  columns: string[];
  message: string;
  onConfirm(): void;
  onCancel(): void;
}

/** Server-reported collision: confirmation resubmits with replace_existing. */
export function OutputColumnCollisionModal({ columns, message, onConfirm, onCancel }: OutputColumnCollisionModalProps) {
  const noun = columns.length === 1 ? 'column' : 'columns';
  return <ConfirmDialog title={`Replace existing ${noun}?`}
    confirmLabel={`Replace existing ${noun}`} onConfirm={onConfirm} onCancel={onCancel}
    testId="overwrite-collision-modal" confirmTestId="overwrite-collision-confirm" cancelTestId="overwrite-collision-cancel">
    <p className="modal-body-text">
      {columns.length > 0
        ? `This will replace the existing "${columns.join(', ')}" ${noun}.`
        : message || 'This will replace existing generated columns.'}
    </p>
  </ConfirmDialog>;
}
