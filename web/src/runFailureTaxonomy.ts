// Failure-triage view of a run's row-error summary: the backend classifies
// every failed cell into a result-outcome taxonomy bucket (store/runs.py:
// model_error / invalid_output / empty_output) and each RunRowErrorGroup
// carries its bucket + terminality on the wire. This module only aggregates
// and labels those buckets — it never parses error message text.

import type { RunRowErrorGroup } from './api/open';

const FAILURE_OUTCOME_LABELS: Record<string, string> = {
  model_error: 'model errors',
  invalid_output: 'invalid output',
  empty_output: 'empty output',
};

/** Human label for a taxonomy bucket; unknown/unclassified buckets fall back
 *  to the raw outcome string so a future backend bucket still renders. */
export function failureOutcomeLabel(outcome: string | null): string {
  if (!outcome) return 'failed';
  return FAILURE_OUTCOME_LABELS[outcome] ?? outcome.replaceAll('_', ' ');
}

export interface FailureBucket {
  /** Taxonomy outcome, or null when the backend sent no classification. */
  outcome: string | null;
  label: string;
  count: number;
  /** Terminal bucket (empty_output): the failure was real and automation
   *  never retries it — only a deliberate user retry re-runs those rows. */
  terminal: boolean;
}

/** Aggregate message-level error groups into outcome buckets, largest first.
 *  Counts cover the server's grouped messages (group list is server-capped,
 *  so the run-level totalFailedRows may exceed the bucket sum). */
export function bucketRowErrors(groups: RunRowErrorGroup[]): FailureBucket[] {
  const buckets = new Map<string | null, FailureBucket>();
  for (const group of groups) {
    const key = group.outcome;
    const bucket = buckets.get(key);
    if (bucket) {
      bucket.count += group.count;
    } else {
      buckets.set(key, {
        outcome: key,
        label: failureOutcomeLabel(key),
        count: group.count,
        terminal: group.terminal,
      });
    }
  }
  return [...buckets.values()].sort((a, b) => b.count - a.count);
}
