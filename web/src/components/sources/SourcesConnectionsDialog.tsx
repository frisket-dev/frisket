import { useEffect, useRef } from 'react';
import { Database } from 'lucide-react';
import { WindowCloseButton } from '../WindowCloseButton';
import { SourcesPanel, type SourcesPanelProps } from '../SourcesPanel';
import './SourcesConnectionsDialog.css';

export interface SourcesConnectionsDialogProps extends SourcesPanelProps {
  open: boolean;
  onClose(): void;
}

/**
 * Focused project-level home for recurring sources and external connections.
 * The management body remains SourcesPanel, so modal and secondary Discover
 * placements share one list/status/edit/poll/delete implementation.
 */
export function SourcesConnectionsDialog({
  open,
  onClose,
  ...panelProps
}: SourcesConnectionsDialogProps) {
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
      className="sources-connections-layer"
      data-testid="sources-connections-dialog"
      aria-labelledby="sources-connections-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <button
        type="button"
        className="sources-connections-backdrop"
        aria-label="Close Sources & connections"
        onClick={onClose}
      />
      <section className="sources-connections" data-testid="sources-connections">
        <header className="sources-connections-header">
          <div className="sources-connections-heading">
            <span className="sources-connections-icon"><Database size={18} aria-hidden /></span>
            <div>
              <h2 id="sources-connections-title">Sources &amp; connections</h2>
              <p>Manage recurring feeds, polling, and connection health for this project.</p>
            </div>
          </div>
          <WindowCloseButton
            ref={closeButtonRef}
            size="compact"
            label="Close Sources & connections"
            data-testid="sources-connections-close"
            onClick={onClose}
          />
        </header>
        <main className="sources-connections-content">
          <SourcesPanel {...panelProps} />
        </main>
      </section>
    </dialog>
  );
}
