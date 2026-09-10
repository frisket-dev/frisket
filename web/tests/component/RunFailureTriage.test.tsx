// @vitest-environment jsdom
//
// Failure triage buckets ("show me everything that failed in this run,
// bucketed by why, and let me act on a bucket"): outcome buckets with
// per-bucket Show/Retry, the honest terminal empty_output copy (retry
// allowed, never oversold), and no actions when the target column is
// unknown or a bucket has no taxonomy outcome.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RunFailureTriage } from '../../src/components/RunFailureTriage';
import type { RunRowErrorGroup, RunRowErrorSummary } from '../../src/api/types';



afterEach(() => {
  cleanup();
});

const group = (over: Partial<RunRowErrorGroup> = {}): RunRowErrorGroup => ({
  message: 'provider rate limited',
  count: 8,
  code: 'provider_rate_limited',
  outcome: 'model_error',
  terminal: false,
  rowIds: ['1', '2'],
  ...over,
});

const summary = (groups: RunRowErrorGroup[], totalFailedRows?: number): RunRowErrorSummary => ({
  totalFailedRows: totalFailedRows ?? groups.reduce((n, g) => n + g.count, 0),
  groups,
});

const THREE_BUCKETS = summary([
  group(),
  group({
    message: 'no output over a non-empty source',
    count: 3,
    code: 'empty_output',
    outcome: 'empty_output',
    terminal: true,
  }),
  group({
    message: 'schema validation failed',
    count: 1,
    code: 'invalid_output',
    outcome: 'invalid_output',
  }),
]);

describe('RunFailureTriage', () => {
  it('renders one bucket per outcome with counts, largest first', () => {
    render(<RunFailureTriage rowErrors={THREE_BUCKETS} />);
    expect(screen.getByTestId('run-failure-triage')).toHaveTextContent('12 failed rows');
    const buckets = screen.getAllByTestId(/^run-failure-bucket-/);
    expect(buckets.map((el) => el.getAttribute('data-testid'))).toEqual([
      'run-failure-bucket-model_error',
      'run-failure-bucket-empty_output',
      'run-failure-bucket-invalid_output',
    ]);
    const modelErrors = screen.getByTestId('run-failure-bucket-model_error');
    expect(modelErrors).toHaveTextContent('8 ×');
    expect(modelErrors).toHaveTextContent('model errors');
  });

  it('Show rows filters to the bucket; Show all failed uses the any bucket', () => {
    const onShowRows = vi.fn();
    render(<RunFailureTriage rowErrors={THREE_BUCKETS} onShowRows={onShowRows} />);
    fireEvent.click(screen.getByTestId('run-failure-bucket-show-empty_output'));
    expect(onShowRows).toHaveBeenLastCalledWith('empty_output');
    fireEvent.click(screen.getByTestId('run-failure-show-all'));
    expect(onShowRows).toHaveBeenLastCalledWith('any');
  });

  it('retryable bucket: plain "Retry N rows"; retry fires once then reports', () => {
    const onRetryRows = vi.fn();
    render(<RunFailureTriage rowErrors={THREE_BUCKETS} onRetryRows={onRetryRows} />);
    const retry = screen.getByTestId('run-failure-bucket-retry-model_error');
    expect(retry).toHaveTextContent('Retry 8 rows');
    fireEvent.click(retry);
    expect(onRetryRows).toHaveBeenCalledWith('model_error');
    // The button reports the request and stops re-firing.
    expect(retry).toBeDisabled();
    expect(retry).toHaveTextContent('Retry requested');
    fireEvent.click(retry);
    expect(onRetryRows).toHaveBeenCalledTimes(1);
  });

  it('terminal empty_output bucket: honest copy, "anyway" wording, retry still allowed', () => {
    const onRetryRows = vi.fn();
    render(<RunFailureTriage rowErrors={THREE_BUCKETS} onRetryRows={onRetryRows} />);
    expect(screen.getByTestId('run-failure-terminal-note')).toHaveTextContent(
      'the failure was real, and retrying may well do the same',
    );
    const retry = screen.getByTestId('run-failure-bucket-retry-empty_output');
    expect(retry).toHaveTextContent('Retry 3 anyway');
    fireEvent.click(retry);
    expect(onRetryRows).toHaveBeenCalledWith('empty_output');
  });

  it('without callbacks (unknown target column) buckets render with no actions', () => {
    render(<RunFailureTriage rowErrors={THREE_BUCKETS} />);
    expect(screen.getByTestId('run-failure-bucket-model_error')).toBeInTheDocument();
    expect(screen.queryByTestId('run-failure-show-all')).not.toBeInTheDocument();
    expect(screen.queryByTestId('run-failure-bucket-retry-model_error')).not.toBeInTheDocument();
  });

  it('an unclassified bucket shows its count but offers no predicate-backed actions', () => {
    render(
      <RunFailureTriage
        rowErrors={summary([group({ outcome: null, terminal: false, count: 2 })])}
        onShowRows={vi.fn()}
        onRetryRows={vi.fn()}
      />,
    );
    const unclassified = screen.getByTestId('run-failure-bucket-unclassified');
    expect(unclassified).toHaveTextContent('2 ×');
    expect(unclassified).toHaveTextContent('failed');
    expect(screen.queryByTestId('run-failure-bucket-show-unclassified')).not.toBeInTheDocument();
    expect(screen.queryByTestId('run-failure-bucket-retry-unclassified')).not.toBeInTheDocument();
  });
});
