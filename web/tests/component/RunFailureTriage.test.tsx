// @vitest-environment jsdom
//
// Failure reasons remain readable; actions apply to all failed rows.

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

  it('offers only Show all failed and Retry, both applying to every failure outcome', () => {
    const onShowRows = vi.fn();
    const onRetryRows = vi.fn();
    render(<RunFailureTriage rowErrors={THREE_BUCKETS} onShowRows={onShowRows} onRetryRows={onRetryRows} />);
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['Show all failed', 'Retry']);
    fireEvent.click(screen.getByRole('button', { name: 'Show all failed' }));
    expect(onShowRows).toHaveBeenCalledWith('any');
    const retry = screen.getByRole('button', { name: 'Retry' });
    fireEvent.click(retry);
    expect(onRetryRows).toHaveBeenCalledWith('any');
    expect(retry).toBeDisabled();
    expect(retry).toHaveTextContent('Retry requested');
    fireEvent.click(retry);
    expect(onRetryRows).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('run-failure-terminal-note')).toHaveTextContent(
      'the failure was real, and retrying may well do the same',
    );
  });

  it('without callbacks (unknown target column) buckets render with no actions', () => {
    render(<RunFailureTriage rowErrors={THREE_BUCKETS} />);
    expect(screen.getByTestId('run-failure-bucket-model_error')).toBeInTheDocument();
    expect(screen.queryByTestId('run-failure-show-all')).not.toBeInTheDocument();
    expect(screen.queryByTestId('run-failure-retry-all')).not.toBeInTheDocument();
  });

  it('an unclassified bucket keeps its count and the overall actions', () => {
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
    expect(unclassified.querySelector('button')).toBeNull();
    expect(screen.getByRole('button', { name: 'Show all failed' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });
});
