import type { KeyboardEvent } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import type { PdfPacketMatchResult, PdfPacketPageMatch } from '../../../api/pdfPacketSplits';
import { matchForPage } from './model';
import { PacketThumbnail } from './PacketThumbnail';

export type ReviewTab = 'unsure' | 'suggested';

function candidateCaption(page: number, match: PdfPacketPageMatch | null): string {
  if (match?.matched_phrase_ids.length) return `Page ${page} · text match`;
  if (match?.visual_score != null) return `Page ${page} · ${Math.round(match.visual_score)}% similar`;
  return `Page ${page}`;
}

export function PacketReviewCard({
  projectId,
  splitId,
  tab,
  index,
  unsure,
  suggested,
  matches,
  onTab,
  onIndex,
  onAccept,
  onReject,
  onAcceptAll,
  canAcceptAll,
  onOpenLarge,
}: {
  projectId: string;
  splitId: string;
  tab: ReviewTab;
  index: number;
  unsure: readonly number[];
  suggested: readonly number[];
  matches: PdfPacketMatchResult | null;
  onTab(tab: ReviewTab): void;
  onIndex(index: number): void;
  onAccept(page: number): void;
  onReject(page: number): void;
  onAcceptAll(): void;
  canAcceptAll: boolean;
  onOpenLarge(page: number): void;
}) {
  const queue = tab === 'unsure' ? unsure : suggested;
  const currentIndex = Math.min(index, Math.max(0, queue.length - 1));
  const page = queue[currentIndex] ?? null;
  const match = page == null ? null : matchForPage(matches, page);
  const move = (delta: number) => {
    if (!queue.length) return;
    onIndex((currentIndex + delta + queue.length) % queue.length);
  };
  const answer = (accept: boolean) => {
    if (page == null) return;
    if (accept) onAccept(page);
    else onReject(page);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const key = event.key.toLowerCase();
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.repeat && (key === 'y' || key === 'n')) {
      event.preventDefault();
      return;
    }
    if (key === 'y' || key === 'n' || event.key === '[' || event.key === ']') {
      event.preventDefault();
      event.stopPropagation();
    }
    if (key === 'y') answer(true);
    else if (key === 'n') answer(false);
    else if (event.key === '[') move(-1);
    else if (event.key === ']') move(1);
  };

  return (
    <section
      className="packet-review-card"
      data-review-tab={tab}
      aria-label={`${tab === 'unsure' ? 'Unsure' : 'Suggested'} pages to review`}
      tabIndex={0}
      onKeyDown={onKeyDown}
    >
      <div className="packet-review-tabs" role="group" aria-label="Review queue">
        <button type="button" aria-pressed={tab === 'unsure'} onClick={() => onTab('unsure')}>
          Unsure <strong>{unsure.length}</strong>
        </button>
        <button type="button" aria-pressed={tab === 'suggested'} onClick={() => onTab('suggested')}>
          Suggested <strong>{suggested.length}</strong>
        </button>
      </div>
      {page == null ? (
        <p className="packet-review-empty">
          {tab === 'unsure' ? 'No unsure pages left.' : 'No suggestions left.'}
        </p>
      ) : (
        <>
          <header className="packet-review-heading">
            <strong>{tab === 'unsure' ? 'Does a document start here?' : 'Likely a start'}</strong>
            <span>{currentIndex + 1} of {queue.length}</span>
          </header>
          <div className="packet-review-pages">
            {page > 1 ? (
              <figure className="packet-review-before">
                <PacketThumbnail projectId={projectId} splitId={splitId} page={page - 1} alt={`Page ${page - 1}`} />
                <figcaption>p {page - 1} · before</figcaption>
              </figure>
            ) : <span className="packet-review-no-before">First page</span>}
            <button
              type="button"
              className="packet-review-candidate"
              aria-label={`View page ${page} large`}
              onClick={() => onOpenLarge(page)}
            >
              <PacketThumbnail projectId={projectId} splitId={splitId} page={page} alt={`Page ${page}`} eager />
              <span>{candidateCaption(page, match)}</span>
            </button>
          </div>
          <div className="packet-review-actions">
            <button
              type="button"
              className="packet-review-step"
              aria-label={`Previous ${tab} page`}
              onClick={() => move(-1)}
            >
              <ChevronLeft size={14} />
            </button>
            <button type="button" className="btn btn-primary" onClick={() => answer(true)}>
              {tab === 'unsure' ? 'Yes, starts here' : 'Accept'}
            </button>
            <button type="button" className="btn" onClick={() => answer(false)}>
              {tab === 'unsure' ? 'No' : 'Not a start'}
            </button>
            <button
              type="button"
              className="packet-review-step"
              aria-label={`Skip to next ${tab} page`}
              onClick={() => move(1)}
            >
              <ChevronRight size={14} />
            </button>
          </div>
        </>
      )}
      {tab === 'suggested' && suggested.length > 0 ? (
        <button type="button" className="packet-review-accept-all" disabled={!canAcceptAll} onClick={onAcceptAll}>
          Accept all {suggested.length} suggestions
        </button>
      ) : null}
    </section>
  );
}
