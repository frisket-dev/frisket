import { useState } from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, X } from 'lucide-react';
import type { SearchPreparationNotice as Notice } from './useSearchPreparationNotice';

export function SearchPreparationNotice({
  notice,
  onReviewApproval,
  onDismiss,
}: {
  notice: Notice;
  onReviewApproval?: () => void;
  onDismiss?(): void;
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const active = notice.tone === 'active';
  const progressLabel = notice.stage === 'download'
    ? notice.detail
    : notice.stage === 'prepare'
      ? 'Preparing files for search'
      : 'Preparing search';

  return (
    <section
      className={`active-run-banner search-preparation-banner status-${notice.tone}`}
      data-testid="search-preparation-banner"
      data-stage={notice.stage}
      role={notice.tone === 'error' ? 'alert' : 'status'}
      aria-live={notice.tone === 'error' ? 'assertive' : 'polite'}
      aria-atomic="true"
    >
      {onDismiss && (
        <button
          type="button"
          className="icon-btn status-notice-dismiss"
          data-testid="search-preparation-dismiss"
          aria-label="Dismiss activity notification"
          onClick={onDismiss}
        >
          <X size={14} aria-hidden />
        </button>
      )}
      <div className="active-run-main">
        {notice.tone === 'error' || notice.tone === 'warning' ? (
          <AlertTriangle size={16} className="run-warning-icon" aria-hidden />
        ) : notice.tone === 'complete' ? (
          <CheckCircle2 size={16} className="search-preparation-complete-icon" aria-hidden />
        ) : (
          <span className="run-spinner" aria-hidden />
        )}
        <div className="active-run-copy">
          <strong>{notice.title}</strong>
          <span>{notice.detail}</span>
        </div>
      </div>

      {active && (notice.stage === 'download' || notice.progress !== null) && (
        <div className="search-preparation-progress" data-testid="search-preparation-progress">
          <span
            className={`model-pull-progress-bar${notice.progress === null ? ' indeterminate' : ''}`}
            role="progressbar"
            aria-label={progressLabel}
            aria-valuemin={0}
            aria-valuemax={100}
            {...(notice.progress !== null ? { 'aria-valuenow': notice.progress } : {})}
          >
            <span
              className="model-pull-progress-fill"
              style={notice.progress !== null ? { width: `${notice.progress}%` } : undefined}
            />
          </span>
          {notice.progress !== null && <span>{notice.progress}%</span>}
        </div>
      )}

      {notice.details.length > 0 && (
        <div className="search-preparation-details">
          <button
            type="button"
            className="search-preparation-details-toggle"
            data-testid="search-preparation-details"
            aria-expanded={detailsOpen}
            onClick={() => setDetailsOpen((open) => !open)}
          >
            {detailsOpen ? <ChevronUp size={12} aria-hidden /> : <ChevronDown size={12} aria-hidden />}
            Details
          </button>
          {detailsOpen && (
            <ul>
              {notice.details.map((detail, index) => <li key={`${index}:${detail}`}>{detail}</li>)}
            </ul>
          )}
        </div>
      )}
      {notice.stage === 'approval' && onReviewApproval && (
        <button
          type="button"
          className="btn btn-compact search-preparation-review"
          data-testid="search-preparation-review"
          onClick={onReviewApproval}
        >
          Review preparation
        </button>
      )}
    </section>
  );
}
