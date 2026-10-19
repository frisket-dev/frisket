import { Button } from './Button';
import { useEffect, useId, useRef, type ReactNode } from 'react';

/** Shared modal lifecycle and framing. Omit onClose for a required step. */
export function Modal({ title, children, onClose, busy = false, testId, role = 'dialog', descriptionId }: {
  title: ReactNode;
  children: ReactNode;
  onClose?: () => void;
  busy?: boolean;
  testId?: string;
  role?: 'dialog' | 'alertdialog';
  descriptionId?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
    return () => { if (dialog?.open) dialog.close(); };
  }, []);
  return <dialog ref={ref} className="modal-card" role={role} aria-labelledby={titleId} aria-describedby={descriptionId}
    data-testid={testId} onCancel={event => { event.preventDefault(); event.stopPropagation(); if (!busy) onClose?.(); }}>
    <div className="modal-title" id={titleId}>{title}</div>
    {children}
  </dialog>;
}

export function ConfirmDialog({ title, children, onConfirm, onCancel, confirmLabel = 'Confirm',
  cancelLabel = 'Cancel', busy = false, blocked = false, danger = false, testId, confirmTestId, cancelTestId,
}: {
  title: ReactNode;
  children: ReactNode;
  onConfirm(): void;
  onCancel(): void;
  confirmLabel?: ReactNode;
  cancelLabel?: string;
  busy?: boolean;
  blocked?: boolean;
  danger?: boolean;
  testId?: string;
  confirmTestId?: string;
  cancelTestId?: string;
}) {
  const descriptionId = useId();
  return <Modal title={title} onClose={onCancel} busy={busy} testId={testId} descriptionId={descriptionId}>
    <form onSubmit={event => { event.preventDefault(); if (!busy && !blocked) onConfirm(); }}>
      <div id={descriptionId}>{children}</div>
      <div className="form-actions">
        <Button type="button" autoFocus disabled={busy} onClick={onCancel} data-testid={cancelTestId}>{cancelLabel}</Button>
        {!blocked && <Button type="submit" className={`btn ${danger ? 'btn-reject' : 'btn-primary'}`}
          disabled={busy} data-testid={confirmTestId}>{confirmLabel}</Button>}
      </div>
    </form>
  </Modal>;
}
