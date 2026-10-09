import { useCallback, useEffect, useRef, type KeyboardEvent } from 'react';
import { Check, ChevronLeft, ChevronRight, X } from 'lucide-react';
import type { PdfPacketMatchResult, PdfPacketPageMatch } from '../../../api/pdfPacketSplits';
import { projectBlobUrl } from '../../../api/raw/projectResources';
import { PdfViewer } from '../../../media/PdfViewer';
import { matchForPage } from './model';
import { PacketThumbnail } from './PacketThumbnail';

const ignorePdfLoaded = () => undefined;

function pageStatus(
  page: number,
  confirmed: readonly number[],
  rejected: readonly number[],
  unsure: readonly number[],
  suggested: readonly number[],
  match: PdfPacketPageMatch | null,
): { kind: 'confirmed' | 'suggested' | 'unsure' | 'rejected' | 'other'; label: string } {
  if (confirmed.includes(page)) return { kind: 'confirmed', label: 'Start of a document' };
  if (rejected.includes(page)) return { kind: 'rejected', label: 'Marked not a start' };
  if (suggested.includes(page)) {
    return {
      kind: 'suggested',
      label: `Suggested start${match?.matched_phrase_ids.length ? ' · text match' : match?.visual_score == null ? '' : ` · ${Math.round(match.visual_score)}%`}`,
    };
  }
  if (unsure.includes(page)) {
    return {
      kind: 'unsure',
      label: `Unsure${match?.visual_score == null ? '' : ` · ${Math.round(match.visual_score)}%`}`,
    };
  }
  return { kind: 'other', label: 'Continuation page' };
}

export function PacketLargePageView({
  projectId,
  blobHash,
  splitId,
  page,
  pageCount,
  confirmed,
  rejected,
  unsure,
  suggested,
  matches,
  onPage,
  onConfirm,
  onReject,
  onRemove,
  onClose,
}: {
  projectId: string;
  blobHash: string;
  splitId: string;
  page: number;
  pageCount: number;
  confirmed: readonly number[];
  rejected: readonly number[];
  unsure: readonly number[];
  suggested: readonly number[];
  matches: PdfPacketMatchResult | null;
  onPage(page: number): void;
  onConfirm(page: number): void;
  onReject(page: number): void;
  onRemove(page: number): void;
  onClose(): void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const match = matchForPage(matches, page);
  const status = pageStatus(page, confirmed, rejected, unsure, suggested, match);
  const isConfirmed = confirmed.includes(page);
  const canReject = suggested.includes(page) || unsure.includes(page);
  const go = useCallback((delta: number) => {
    onPage(Math.max(1, Math.min(pageCount, page + delta)));
  }, [onPage, page, pageCount]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return undefined;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    dialog.focus();
    return () => {
      if (dialog.open && typeof dialog.close === 'function') dialog.close();
    };
  }, []);

  const close = () => {
    // Close while mounted so the browser restores focus to the opener.
    dialogRef.current?.close();
    onClose();
  };

  const secondaryAction = () => {
    if (isConfirmed && page !== 1) onRemove(page);
    else if (canReject) onReject(page);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDialogElement>) => {
    const key = event.key.toLowerCase();
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.repeat && (key === 's' || key === 'x')) {
      event.preventDefault();
      return;
    }
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight' || key === 's' || key === 'x' || event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
    }
    if (event.key === 'ArrowLeft') go(-1);
    else if (event.key === 'ArrowRight') go(1);
    else if (key === 's' && !isConfirmed) onConfirm(page);
    else if (key === 'x') secondaryAction();
    else if (event.key === 'Escape') close();
  };

  return (
    <dialog
      ref={dialogRef}
      className="packet-large-view"
      data-testid="pdf-packet-large-view"
      aria-label={`Page ${page}`}
      aria-modal="true"
      onKeyDown={onKeyDown}
      onCancel={(event) => {
        event.preventDefault();
        event.stopPropagation();
        close();
      }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <section className="packet-large-card">
        <header>
          <h2>Page {page}</h2>
          <span className="packet-page-status" data-status={status.kind}>{status.label}</span>
          <button type="button" className="icon-btn" aria-label="Close large page view" onClick={close}>
            <X size={17} />
          </button>
        </header>
        <div className="packet-large-pages" data-status={status.kind}>
          <button type="button" aria-label="Previous page" disabled={page === 1} onClick={() => go(-1)}>
            <ChevronLeft size={18} />
          </button>
          {page > 1 ? (
            <figure className="packet-large-before">
              <PacketThumbnail projectId={projectId} splitId={splitId} page={page - 1} alt={`Page ${page - 1}`} eager />
              <figcaption>p {page - 1} · before</figcaption>
            </figure>
          ) : <span className="packet-large-first">First page</span>}
          <div className="packet-large-current">
            <PdfViewer
              url={projectBlobUrl(projectId, blobHash)}
              layout="single"
              fit="page"
              zoom={1}
              textLayer={false}
              currentPage={page}
              onLoaded={ignorePdfLoaded}
              onCurrentPageChange={onPage}
              className="packet-large-pdf"
            />
            <span>Page {page}</span>
          </div>
          <button type="button" aria-label="Next page" disabled={page === pageCount} onClick={() => go(1)}>
            <ChevronRight size={18} />
          </button>
        </div>
        <footer>
          <div className="packet-large-actions">
            {!isConfirmed ? (
              <button type="button" className="btn btn-primary" onClick={() => onConfirm(page)}>
                <Check size={13} /> Starts a document <kbd>S</kbd>
              </button>
            ) : null}
            {isConfirmed && page !== 1 ? (
              <button type="button" className="btn" onClick={() => onRemove(page)}>
                <X size={13} /> Remove start <kbd>X</kbd>
              </button>
            ) : canReject ? (
              <button type="button" className="btn" onClick={() => onReject(page)}>
                <X size={13} /> Not a start <kbd>X</kbd>
              </button>
            ) : null}
          </div>
          <span>← → pages · Esc close</span>
        </footer>
      </section>
    </dialog>
  );
}
