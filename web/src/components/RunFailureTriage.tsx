import { useState } from 'react';
import type { RunRowErrorSummary } from '../api/open';
import { bucketRowErrors } from '../runFailureTaxonomy';
import { MediaProxyRemediationCard } from './MediaProxyRemediationCard';
import './run-failure-triage.css';

/** Row-error code the backend attaches to a media-download group whose rows
 *  all failed because YouTube blocked the server's egress IP (see
 *  RunRowErrorGroup.code). Its remediation card renders above the generic
 *  buckets, not as one of them. */
const YOUTUBE_PROVIDER_BLOCKED_CODE = 'youtube_provider_blocked';

// Failure triage for a finished run: the run's row errors bucketed by the
// backend's result-outcome taxonomy (never by parsing message text), with
// per-bucket actions — filter the grid to the failing rows, or retry exactly
// that bucket through the deliberate-retry backfill.
//
// Copy stance for the terminal empty_output bucket:
// the failure was real and a retry may not change the outcome — the affordance
// exists but is never oversold as a fix.
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
  /** Retry exactly this bucket's rows (run.backfill row_ids). */
  onRetryRows?: (outcome: string) => void;
}) {
  const buckets = bucketRowErrors(rowErrors.groups);
  const blockedGroup = rowErrors.groups.find(
    (group) => group.code === YOUTUBE_PROVIDER_BLOCKED_CODE,
  );
  // One retry in flight at a time; the button stays honest about what it did.
  const [retriedOutcomes, setRetriedOutcomes] = useState<ReadonlySet<string>>(
    new Set(),
  );
  if (buckets.length === 0) return null;

  const retry = (outcome: string) => {
    if (!onRetryRows) return;
    setRetriedOutcomes((current) => new Set([...current, outcome]));
    onRetryRows(outcome);
  };

  return (
    <div className="run-failure-triage" data-testid="run-failure-triage">
      {blockedGroup && (
        <MediaProxyRemediationCard group={blockedGroup} onRetryRows={onRetryRows} />
      )}
      <div className="run-failure-triage-head">
        <span className="run-failure-triage-title">
          {rowErrors.totalFailedRows.toLocaleString()} failed{' '}
          {rowErrors.totalFailedRows === 1 ? 'row' : 'rows'}
        </span>
        {onShowRows && (
          <button
            type="button"
            className="mini-btn"
            data-testid="run-failure-show-all"
            onClick={() => onShowRows('any')}
          >
            Show all failed
          </button>
        )}
      </div>
      <ul className="run-failure-buckets">
        {buckets.map((bucket) => {
          // A bucket without a taxonomy outcome cannot be expressed as a
          // server-side predicate — render its count, offer no actions.
          const outcome = bucket.outcome;
          const retried = outcome !== null && retriedOutcomes.has(outcome);
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
              {outcome !== null && onShowRows && (
                <button
                  type="button"
                  className="mini-btn"
                  data-testid={`run-failure-bucket-show-${outcome}`}
                  onClick={() => onShowRows(outcome)}
                >
                  Show rows
                </button>
              )}
              {outcome !== null && onRetryRows && (
                <button
                  type="button"
                  className="mini-btn"
                  data-testid={`run-failure-bucket-retry-${outcome}`}
                  disabled={retried}
                  onClick={() => retry(outcome)}
                >
                  {retried
                    ? 'Retry requested'
                    : bucket.terminal
                      ? `Retry ${bucket.count.toLocaleString()} anyway`
                      : `Retry ${bucket.count.toLocaleString()} ${bucket.count === 1 ? 'row' : 'rows'}`}
                </button>
              )}
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
