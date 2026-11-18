import { useEffect, useRef } from 'react';
import { Database } from 'lucide-react';
import { WindowCloseButton } from '../WindowCloseButton';
import { FeedsPanel, type FeedsPanelProps } from '../FeedsPanel';
import './FeedsConnectionsDialog.css';

interface FeedsConnectionsDialogProps extends FeedsPanelProps {
  open: boolean;
  onClose(): void;
}

/**
 * Focused project-level home for recurring sources and external connections.
 * The management body remains FeedsPanel, so modal and secondary Discover
 * placements share one list/status/edit/poll/delete implementation.
 */
export function FeedsConnectionsDialog({
  open,
  onClose,
  ...panelProps
}: FeedsConnectionsDialogProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    const focusFrame = window.requestAnimationFrame(() => {
      closeButtonRef.current?.focus();
    });
    return () => window.cancelAnimationFrame(focusFrame);
  }, [open]);

  if (!open) return null;

  return (
    <dialog
      ref={dialogRef}
      className="feeds-connections-layer"
      data-testid="feeds-connections-dialog"
      aria-labelledby="sources-connections-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <button
        type="button"
        className="feeds-connections-backdrop"
        aria-label="Close Feeds & connections"
        onClick={onClose}
      />
      <section className="feeds-connections" data-testid="feeds-connections">
        <header className="feeds-connections-header">
          <div className="feeds-connections-heading">
            <span className="feeds-connections-icon"><Database size={18} aria-hidden /></span>
            <div>
              <h2 id="sources-connections-title">Feeds &amp; connections</h2>
              <p>Manage recurring feeds, scheduled checks, and connection health for this project.</p>
            </div>
          </div>
          <WindowCloseButton
            ref={closeButtonRef}
            size="compact"
            label="Close Feeds & connections"
            data-testid="feeds-connections-close"
            onClick={onClose}
          />
        </header>
        <main className="feeds-connections-content">
          <FeedsPanel {...panelProps} />
        </main>
      </section>
    </dialog>
  );
}
