import type { ReactNode } from 'react';
import { Check, ChevronLeft, ChevronRight, Pencil, X } from 'lucide-react';
import { WindowCloseButton } from '../WindowCloseButton';
import { Modal } from '../Modal';
import '../ReviewQueue.css';
import styles from './ReviewWorkspace.module.css';

/** Shared full-screen Review frame; controllers retain their own close/save behavior. */
export function ReviewShell({ children, busy, onClose }: { children: ReactNode; busy: boolean; onClose(): void }) {
  return <Modal title="Review queue" hideHeader className="review-overlay" testId="review-queue" busy={busy} onClose={onClose}>
    {children}
  </Modal>;
}

export function ReviewHeader({ children, busy, onClose, closeLabel = 'Close review queue' }: { children: ReactNode; busy: boolean; onClose(): void; closeLabel?: string }) {
  return <div className="review-run-header">{children}
    <WindowCloseButton size="compact" disabled={busy} onClick={onClose} label={closeLabel} data-modal-autofocus />
  </div>;
}

export function ReviewNavigation({ label, position, total, busy, canPrevious, canNext, onPrevious, onNext }: {
  label: string; position: number; total: number; busy: boolean; canPrevious: boolean; canNext: boolean; onPrevious(): void; onNext(): void;
}) {
  return <nav className={styles.navigation} aria-label={label}>
    <span data-testid="review-page-status">{total ? position : 0} / {total}</span>
    <button type="button" className="icon-btn" aria-label="Previous row" disabled={busy || !canPrevious} onClick={onPrevious}><ChevronLeft size={15} /></button>
    <button type="button" className="icon-btn" aria-label="Next row" disabled={busy || !canNext} onClick={onNext}><ChevronRight size={15} /></button>
  </nav>;
}

export function ReviewWorkspace({ source, children, label, testId, busy }: {
  source: ReactNode; children: ReactNode; label: string; testId: string; busy: boolean;
}) {
  return <main className={styles.workspace} data-testid={testId} aria-busy={busy}>
    {source}<section className={styles.results} data-testid="review-output-panel" aria-label={label}>{children}</section>
  </main>;
}

export function ReviewDecisionControls({ label, acceptLabel, rejectLabel, acceptPressed, rejectPressed,
  acceptDisabled, rejectDisabled, onAccept, onReject, edit }: {
  label: string; acceptLabel: string; rejectLabel: string; acceptPressed: boolean; rejectPressed: boolean;
  acceptDisabled: boolean; rejectDisabled: boolean; onAccept(): void; onReject(): void;
  edit?: { label: string; title: string; pressed: boolean; disabled: boolean; onClick(): void };
}) {
  return <div className={styles.verdict} role="group" aria-label={label}>
    <button type="button" aria-label={acceptLabel} title="Accept (← or A)" aria-pressed={acceptPressed}
      disabled={acceptDisabled} className={styles.accept} onClick={onAccept}><Check size={16} /></button>
    <button type="button" aria-label={rejectLabel} title="Reject (→ or D)" aria-pressed={rejectPressed}
      disabled={rejectDisabled} className={styles.reject} onClick={onReject}><X size={16} /></button>
    {edit && <button type="button" aria-label={edit.label} title={edit.title} aria-pressed={edit.pressed}
      disabled={edit.disabled} className={styles.edit} onClick={edit.onClick}><Pencil size={14} /></button>}
  </div>;
}
