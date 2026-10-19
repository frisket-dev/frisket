import { Button } from './Button';
import { useLayoutEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import './Modal.css';
import { WindowCloseButton } from './WindowCloseButton';

/** Shared modal lifecycle and framing. Omit onClose for a required step. */
export function Modal({ title, children, onClose, busy = false, testId, role = 'dialog', descriptionId, size = 'standard', closeLabel }: {
  title: ReactNode;
  children: ReactNode;
  onClose?: () => void;
  busy?: boolean;
  testId?: string;
  role?: 'dialog' | 'alertdialog';
  descriptionId?: string;
  size?: 'standard' | 'wide';
  closeLabel?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const mounted = useRef(false);
  const titleId = useId();
  useLayoutEffect(() => {
    mounted.current = true;
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
    dialog?.querySelector<HTMLElement>('[data-modal-autofocus]')?.focus();
    return () => { mounted.current = false; if (dialog?.open) dialog.close(); };
  }, []);
  // A modal must escape hidden keepalive panes as well as their layout styles.
  return createPortal(<dialog ref={ref} className="modal-card" data-size={size} role={role} aria-labelledby={titleId} aria-describedby={descriptionId}
    data-testid={testId} onCancel={event => {
      if (event.target !== event.currentTarget) return;
      event.preventDefault(); event.stopPropagation();
      if (event.cancelable && !busy) onClose?.();
    }} onClose={event => {
      if (event.target !== event.currentTarget) return;
      event.stopPropagation();
      // Ignore a queued close from a prior lifecycle (including StrictMode).
      if (!mounted.current || event.currentTarget.open) return;
      // Browsers can force closure after repeated Escape, despite preventDefault.
      if (busy || !onClose) { if (!event.currentTarget.open) event.currentTarget.showModal(); }
      else onClose();
    }}>
    <div className="modal-header">
      <div className="modal-title" id={titleId}>{title}</div>
      {onClose && closeLabel && <WindowCloseButton label={closeLabel} disabled={busy} onClick={onClose} />}
    </div>
    {children}
  </dialog>, document.body);
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
        <Button type="button" data-modal-autofocus disabled={busy} onClick={onCancel} data-testid={cancelTestId}>{cancelLabel}</Button>
        {!blocked && <Button type="submit" className={`btn ${danger ? 'btn-reject' : 'btn-primary'}`}
          disabled={busy} data-testid={confirmTestId}>{confirmLabel}</Button>}
      </div>
    </form>
  </Modal>;
}
