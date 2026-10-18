import { useState } from 'react';
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, X } from 'lucide-react';
import type { SearchPreparationNotice as Notice } from './useSearchPreparationNotice';

export interface ActivityProgressLine {
  key: string;
  label: string;
  completed: number | null;
  total: number | null;
  detail?: string;
  complete?: boolean;
}

export function SearchPreparationNotice({
  notice,
  onReviewPreparation,
  onDismiss,
  onStopPreparation,
  progressLines,
  now,
  stopLabel = 'Stop remaining preparation',
  onResumeImport,
  onKeepImport,
  importActionBusy = false,
  importMessage,
}: {
  notice: Notice;
  onReviewPreparation?: (batchId: string) => void;
  onDismiss?(): void;
  onStopPreparation?(): Promise<void>;
  progressLines?: ActivityProgressLine[];
  now?: string | null;
  stopLabel?: string;
  onResumeImport?(): void;
  onKeepImport?(): void;
  importActionBusy?: boolean;
  importMessage?: string | null;
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [stopState, setStopState] = useState<{ batchId: string; busy: boolean; error: string | null } | null>(null);
  const currentStop = stopState?.batchId === notice.stopBatchId ? stopState : null;
  const stop = async () => {
    const batchId = notice.stopBatchId;
    if (!batchId || !onStopPreparation || currentStop?.busy) return;
    setStopState({ batchId, busy: true, error: null });
    try {
      await onStopPreparation();
      setStopState((current) => current?.batchId === batchId ? { ...current, busy: false } : current);
    } catch (error) {
      setStopState((current) => current?.batchId === batchId ? {
        batchId, busy: false, error: error instanceof Error ? error.message : String(error),
      } : current);
    }
  };
  const active = notice.tone === 'active';
  const progressLabel = notice.stage === 'download'
    ? notice.detail
    : notice.stage === 'prepare'
      ? 'Preparing files for search'
      : 'Preparing search';
  const searchLine = progressLines?.find((line) => line.key === 'search-index') ?? null;
  const searchFooter = searchLine
    ? notice.tone === 'error'
      ? 'Search preparation needs attention.'
      : searchLine.complete
      ? 'Ready files are searchable.'
      : 'Files become searchable as preparation finishes.'
    : null;
  const canStop = Boolean(notice.stopBatchId && onStopPreparation);
  const canReview = Boolean(
    notice.reviewBatchId && onReviewPreparation
      && (notice.stage === 'approval' || notice.stage === 'failed'),
  );
  const hasFooter = Boolean(
    searchFooter || canStop || onResumeImport || onKeepImport || importMessage || canReview,
  );

  return (
    <section
      className={`active-run-banner search-preparation-banner status-${notice.tone}`}
      data-testid="search-preparation-banner"
      data-stage={notice.stage}
      role={notice.tone === 'error' ? 'alert' : 'status'}
      aria-live={notice.tone === 'error' ? 'assertive' : 'polite'}
      aria-atomic="true"
    >
      <div className="search-preparation-header">
        <div className="active-run-main">
          {notice.tone === 'error' || notice.tone === 'warning' ? (
            <AlertTriangle size={16} className="run-warning-icon" aria-hidden />
          ) : notice.tone === 'complete' ? (
            <CheckCircle2 size={16} className="search-preparation-complete-icon" aria-hidden />
          ) : null}
          <div className="active-run-copy">
            <strong>{notice.title}</strong>
            <span>{notice.detail}</span>
          </div>
        </div>
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
      </div>

      <div className="search-preparation-body">
        {!!progressLines?.length && (
          <div className="activity-progress-list" role="group" aria-label="Import preparation progress">
            {progressLines.map((line) => {
              const boundedTotal = line.total !== null && line.total > 0 ? line.total : null;
              const completed = boundedTotal === null || line.completed === null
                ? null
                : Math.max(0, Math.min(line.completed, boundedTotal));
              const done = line.complete ?? (
                completed !== null && boundedTotal !== null && completed >= boundedTotal
              );
              const doneLabel = line.key === 'search-index'
                ? '✓ Ready'
                : line.key === 'ocr' || line.key === 'native' ? '✓ Read' : '✓ Done';
              const count = done
                ? doneLabel
                : line.detail ?? (completed !== null && boundedTotal !== null
                  ? `${completed.toLocaleString()} of ${boundedTotal.toLocaleString()}`
                  : 'Waiting');
              return (
                <div className={`activity-progress-row${done ? ' is-complete' : ''}`} key={line.key}>
                  <span className="activity-progress-label">
                    <strong>{line.label}</strong>
                    {boundedTotal !== null && (
                      <span>{boundedTotal.toLocaleString()}{line.key === 'search-index' ? ' pages' : ''}</span>
                    )}
                  </span>
                  {boundedTotal !== null && completed !== null ? (
                    <progress
                      className="run-file-progress-bar"
                      aria-label={`${line.label} progress`}
                      max={boundedTotal}
                      value={completed}
                    />
                  ) : (
                    <span
                      className={`model-pull-progress-bar${done ? ' complete' : ' indeterminate'}`}
                      role="progressbar"
                      aria-label={`${line.label} progress`}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      {...(done ? { 'aria-valuenow': 100 } : {})}
                    ><span className="model-pull-progress-fill" /></span>
                  )}
                  <span className="activity-progress-count">{count}</span>
                </div>
              );
            })}
          </div>
        )}
        {now && <p className="import-activity-now" data-testid="import-activity-now"><strong>Now</strong><span>{now}</span><span>opened by you</span></p>}

        {!progressLines?.length && !notice.pdfFiles?.length
          && ((active && (notice.stage === 'download' || notice.stage === 'index' || notice.progress !== null))
            || (notice.tone === 'error' && notice.progress !== null)) && (
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

        {!!notice.pdfFiles?.length && (
          <div className="pdf-index-progress-list" role="group" aria-label="PDF indexing progress">
            {notice.pdfFiles.map((file) => {
              const filename = file.filename || 'PDF document';
              return (
                <div className="pdf-index-progress" key={file.source_id}>
                  <div className="run-file-progress-copy">
                    <span className="run-file-progress-name" title={filename}>{filename}</span>
                    <span className="run-file-progress-count">
                      {file.completed.toLocaleString()} / {file.total.toLocaleString()} pages · {Math.max(file.total - file.completed, 0).toLocaleString()} remaining
                    </span>
                  </div>
                  <progress
                    className="run-file-progress-bar"
                    aria-label={`Indexing pages for ${filename}`}
                    max={file.total}
                    value={file.completed}
                  />
                </div>
              );
            })}
          </div>
        )}
        {notice.pdfFilesTruncated && <p className="search-preparation-details">More PDFs are being indexed.</p>}

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
      </div>

      {hasFooter && (
        <div className="search-preparation-footer">
          {searchFooter && <span className="search-preparation-footer-copy">{searchFooter}</span>}
          <div className="search-preparation-actions">
            {canStop && (
              <button type="button" className="search-preparation-action" disabled={currentStop?.busy}
                onClick={() => void stop()}>
                {currentStop?.busy ? 'Stopping…' : stopLabel}
              </button>
            )}
            {onResumeImport && (
              <button type="button" className="search-preparation-action" disabled={importActionBusy} onClick={onResumeImport}>
                Resume
              </button>
            )}
            {onKeepImport && (
              <button type="button" className="search-preparation-action" disabled={importActionBusy} onClick={onKeepImport}>
                Keep what's added
              </button>
            )}
            {canReview && (
              <button
                type="button"
                className="search-preparation-action search-preparation-review"
                data-testid="search-preparation-review"
                onClick={() => onReviewPreparation!(notice.reviewBatchId!)}
              >
                {notice.stage === 'approval' ? 'Review text' : 'Review files'}
              </button>
            )}
          </div>
          {(currentStop?.error || importMessage) && (
            <span className="search-preparation-footer-error" role="alert">
              {currentStop?.error || importMessage}
            </span>
          )}
        </div>
      )}
    </section>
  );
}
