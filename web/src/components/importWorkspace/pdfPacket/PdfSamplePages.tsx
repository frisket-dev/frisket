import '../../IconButton.css';
import './PdfSamplePages.css';
import { Button } from '../../Button';
import { RefreshCw } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { resamplePages, samplePages } from './model';

/** Page selection only: callers retain text reads, OCR and preparation consent. */
export function PdfSamplePages({
  pageCount, selectedPage, onSelectPage, renderThumbnail,
}: {
  pageCount: number;
  selectedPage: number;
  onSelectPage(page: number): void;
  renderThumbnail?(page: number): ReactNode;
}) {
  const [defaultSamples, setSamples] = useState(() => samplePages(pageCount));
  const [draft, setDraft] = useState({ page: selectedPage, text: String(selectedPage) });
  const samples = defaultSamples.includes(selectedPage)
    ? defaultSamples
    : [selectedPage, ...defaultSamples.slice(1)].sort((left, right) => left - right);
  const pageInput = draft.page === selectedPage ? draft.text : String(selectedPage);
  const requestedPage = Number(pageInput);
  const validPage = Number.isInteger(requestedPage) && requestedPage >= 1 && requestedPage <= pageCount;
  return (
    <div className="pdf-sample-pages">
      <div className="pdf-packet-sample-heading">
        <h3>Sample pages</h3>
        <button type="button" className="icon-btn" aria-label="Refresh sample pages" title="Refresh sample pages"
          disabled={pageCount <= 6} onClick={() => {
            const next = resamplePages(pageCount, samples);
            setSamples(next);
            onSelectPage(next.find((page) => page !== selectedPage) ?? selectedPage);
          }}><RefreshCw size={15} aria-hidden /></button>
      </div>
      <div className={renderThumbnail ? 'pdf-packet-samples' : 'search-preparation-sample-pages'}>
        {samples.map((page) => (
          <button type="button" key={page} className={page === selectedPage ? 'active' : ''}
            aria-label={`Page ${page}`} aria-pressed={page === selectedPage} onClick={() => onSelectPage(page)}>
            {renderThumbnail?.(page)}
            <span>{renderThumbnail ? `p ${page}` : `Page ${page}`}</span>
          </button>
        ))}
      </div>
      <form className="pdf-packet-page-picker" onSubmit={(event) => {
        event.preventDefault();
        if (validPage) onSelectPage(requestedPage);
      }}>
        <label>
          Page <input type="number" min={1} max={pageCount} value={pageInput} aria-label="Sample page number"
            onChange={(event) => setDraft({ page: selectedPage, text: event.currentTarget.value })} />
          {' '}of {pageCount}
        </label>
        <Button type="submit" disabled={!validPage || requestedPage === selectedPage}>Go</Button>
      </form>
    </div>
  );
}
