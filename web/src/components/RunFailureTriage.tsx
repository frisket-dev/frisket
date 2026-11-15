import { Button } from './Button';
import { useRef, useState } from 'react';
import type { RunRowErrorSummary } from '../api/open';
import { bucketRowErrors } from '../runFailureTaxonomy';
import { MediaProxyRemediationCard } from './MediaProxyRemediationCard';
import './run-failure-triage.css';

/** Row-error code the backend attaches to a media-download group whose rows
 *  all failed because YouTube blocked the server's egress IP (see
 *  RunRowErrorGroup.code). Its remediation card renders above the generic
 *  buckets, not as one of them. */
const YOUTUBE_PROVIDER_BLOCKED_CODE = 'youtube_provider_blocked';

// Keep failure reasons readable while the run offers one filter and one retry.
// The original row-level errors remain available in the run inspector below.
export function RunFailureTriage({
  rowErrors,
  onShowRows,
  onRetryRows,
}: {
  rowErrors: RunRowErrorSummary;
  /** Filter the grid to the target column's failing rows for this bucket
   *  ('any' = every failure bucket). Omitted when the run's target column is
   *  unknown — the buckets still render, the actions do not. */
  onShowRows?: (outcome: string) => void;
  /** Retry failed rows; 'any' includes every failure outcome. */
  onRetryRows?: (outcome: string) => Promise<boolean>;
}) {
  const buckets = bucketRowErrors(rowErrors.groups);
  const blockedGroup = rowErrors.groups.find(
    (group) => group.code === YOUTUBE_PROVIDER_BLOCKED_CODE,
  );
  const [retryState, setRetryState] = useState<'idle' | 'pending' | 'accepted'>('idle');
  const retryInFlight = useRef(false);
  if (buckets.length === 0) return null;

  const retry = async () => {
    if (!onRetryRows || retryInFlight.current || retryState === 'accepted') return;
    retryInFlight.current = true;
    setRetryState('pending');
    try {
      setRetryState(await onRetryRows('any') ? 'accepted' : 'idle');
    } catch {
      setRetryState('idle');
    } finally {
      retryInFlight.current = false;
    }
  };

  return (
    <div className="run-failure-triage" data-testid="run-failure-triage">
      {blockedGroup && (
        <MediaProxyRemediationCard group={blockedGroup} />
      )}
      <div className="run-failure-triage-head">
        <span className="run-failure-triage-title">
          {rowErrors.totalFailedRows.toLocaleString()} failed{' '}
          {rowErrors.totalFailedRows === 1 ? 'row' : 'rows'}
        </span>
        <div className="run-failure-triage-actions">
          {onShowRows && (
            <Button
              type="button"
              data-testid="run-failure-show-all"
              onClick={() => onShowRows('any')}
            >
              Show all failed
            </Button>
          )}
          {onRetryRows && (
            <Button type="button" data-testid="run-failure-retry-all" disabled={retryState !== 'idle'} onClick={() => void retry()}>
              {retryState === 'accepted' ? 'Retry requested' : retryState === 'pending' ? 'Checking retry…' : 'Retry'}
            </Button>
          )}
        </div>
      </div>
      <ul className="run-failure-buckets">
        {buckets.map((bucket) => {
          const outcome = bucket.outcome;
          return (
            <li
              key={outcome ?? 'unclassified'}
              className="run-failure-bucket"
              data-testid={`run-failure-bucket-${outcome ?? 'unclassified'}`}
            >
              <span className="run-failure-bucket-count">
                {bucket.count.toLocaleString()} ×
              </span>
              <span className="run-failure-bucket-label">{bucket.label}</span>
              {bucket.terminal && (
                <div
                  className="run-failure-bucket-note"
                  data-testid="run-failure-terminal-note"
                >
                  These rows ran and returned nothing — the failure was real,
                  and retrying may well do the same.
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
