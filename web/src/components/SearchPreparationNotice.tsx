import { useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import type { SearchPreparationNotice as Notice } from './useSearchPreparationNotice';
import { ActivityProgressCard, ProgressBar, ProgressRow } from './ActivityProgress';

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
    <ActivityProgressCard
      state={notice.tone}
      title={notice.title}
      detail={notice.detail}
      testId="search-preparation-banner"
      stage={notice.stage}
      className="search-preparation-banner"
      onDismiss={onDismiss}
      dismissTestId="search-preparation-dismiss"
      footer={hasFooter ? (
        <>
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
        </>
      ) : undefined}
    >
      <div className="search-preparation-body">
        {!!progressLines?.length && (
          <div className="activity-progress-list" role="group" aria-label="Import preparation progress">
            {progressLines.map((line) => {
              const boundedTotal = line.total !== null && line.total > 0 ? line.total : null;
              const completed = boundedTotal === null || line.completed === null
                ? null
                : Math.max(0, Math.min(line.completed, boundedTotal));
              const done = line.complete === true;
              const doneLabel = line.key === 'search-index'
                ? '✓ Ready'
                : line.key === 'ocr' || line.key === 'native' ? '✓ Read' : '✓ Done';
              const count = done
                ? doneLabel
                : line.detail ?? (completed !== null && boundedTotal !== null
                  ? `${completed.toLocaleString()} of ${boundedTotal.toLocaleString()}`
                  : 'Waiting');
              return <ProgressRow
                key={line.key}
                label={line.label}
                labelMeta={boundedTotal !== null
                  ? `${boundedTotal.toLocaleString()}${line.key === 'search-index' ? ' pages' : ''}`
                  : undefined}
                completed={completed}
                total={boundedTotal}
                state={done ? 'complete' : notice.tone === 'error' ? 'error' : 'active'}
                status={count}
                ariaLabel={`${line.label} progress`}
              />;
            })}
          </div>
        )}
        {now && <p className="import-activity-now" data-testid="import-activity-now"><strong>Now</strong><span>{now}</span><span>opened by you</span></p>}

        {!progressLines?.length && !notice.pdfFiles?.length
          && ((active && (notice.stage === 'download' || notice.stage === 'index' || notice.progress !== null))
            || (notice.tone === 'error' && notice.progress !== null)) && (
          <div className="search-preparation-progress" data-testid="search-preparation-progress">
            <ProgressBar
              label={progressLabel}
              completed={notice.progress}
              total={notice.progress === null ? null : 100}
              state={notice.tone}
            />
            {notice.progress !== null && <span>{notice.progress}%</span>}
          </div>
        )}

        {!!notice.pdfFiles?.length && (
          <div className="pdf-index-progress-list" role="group" aria-label="PDF indexing progress">
            {notice.pdfFiles.map((file) => {
              const filename = file.filename || 'PDF document';
              return (
                <ProgressRow
                  key={file.source_id}
                  className="pdf-index-progress"
                  label={<span title={filename}>{filename}</span>}
                  completed={file.completed}
                  total={file.total}
                  state="active"
                  status={`${file.completed.toLocaleString()} / ${file.total.toLocaleString()} pages · ${Math.max(file.total - file.completed, 0).toLocaleString()} remaining`}
                  ariaLabel={`Indexing pages for ${filename}`}
                />
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

    </ActivityProgressCard>
  );
}
