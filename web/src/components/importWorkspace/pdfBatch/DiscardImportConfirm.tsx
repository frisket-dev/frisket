import { ConfirmDialog } from '../../Modal';

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
  return <ConfirmDialog title={title} onConfirm={onConfirm} onCancel={onKeep} busy={busy} danger
    testId={testId} confirmLabel={confirmLabel} cancelLabel="Keep working">
    <p className="modal-body-text">{message}</p>
  </ConfirmDialog>;
}
