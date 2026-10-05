// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import type { ReviewRun } from '../../src/api/types';
import { ReviewRunControls, ReviewRunSummary } from '../../src/components/review/ReviewRunControls';
import { installDialogPolyfill } from '../support/domPolyfills';

beforeAll(installDialogPolyfill);
afterEach(cleanup);

const run: ReviewRun = {
  runId: '9', sheetId: '3', sheetName: 'Articles', actionKind: 'map.classify',
  actionName: 'Classify', model: 'model-1', startedAt: '2026-09-01T12:00:00Z',
  reviewStatus: 'open', reviewCompletedAt: null,
  total: {
    eligibleCount: 10, reviewedCount: 5, acceptedCount: 3, incorrectCount: 1,
    unreviewedCount: 5, confidenceCount: 7,
  },
  fields: [{
    columnId: '5', columnName: 'Label', columnType: 'text', eligibleCount: 10,
    reviewedCount: 5, acceptedCount: 3, incorrectCount: 1, unreviewedCount: 5,
    confidenceCount: 0,
  }],
};

describe('ReviewRunControls', () => {
  it('uses PanelSelects to scope review to a run and output field', () => {
    const changedRun = vi.fn();
    const changedField = vi.fn();
    render(
      <ReviewRunControls
        runs={[run]}
        selectedRunId="9"
        onSelectedRunChange={changedRun}
        selectedFieldId={null}
        onSelectedFieldChange={changedField}
        order="shuffle"
        onOrderChange={vi.fn()}
      />,
    );

    fireEvent.mouseDown(screen.getByTestId('review-field-select'));
    fireEvent.click(within(screen.getByTestId('review-field-select-menu')).getByRole('option', { name: /Label/ }));
    expect(changedField).toHaveBeenCalledWith('5');
    expect(screen.getByTestId('review-run-progress')).toHaveTextContent('5 of 10 reviewed');
    expect(screen.getByRole('progressbar', { name: 'Review progress' })).toHaveAttribute('aria-valuenow', '5');
    fireEvent.click(screen.getByRole('button', { name: 'Results' }));
    expect(screen.getByTestId('review-results')).toBeInTheDocument();
    expect(screen.getByText('Run: Classify on Articles')).toBeInTheDocument();
    expect(screen.getByTestId('review-run-summary')).toHaveTextContent('75% correct among reviewed');
  });

  it('shows review progress dots in the selected run trigger and each run option', () => {
    const unreviewed = { ...run, total: { ...run.total, reviewedCount: 0 } };
    const partial = { ...run, runId: '10', total: { ...run.total, reviewedCount: 1 } };
    const complete = { ...run, runId: '11', reviewStatus: 'complete' as const };
    render(
      <ReviewRunControls
        runs={[unreviewed, partial, complete]}
        selectedRunId="10"
        onSelectedRunChange={vi.fn()}
        selectedFieldId={null}
        onSelectedFieldChange={vi.fn()}
        order="shuffle"
        onOrderChange={vi.fn()}
      />,
    );

    expect(screen.getByLabelText('Review in progress')).toBeInTheDocument();
    expect(screen.getByTestId('review-run-select')).toHaveAccessibleName('Run: Review in progress');
    fireEvent.mouseDown(screen.getByTestId('review-run-select'));
    expect(within(screen.getByTestId('review-run-select-menu')).getByLabelText('No review decisions yet')).toBeInTheDocument();
    expect(within(screen.getByTestId('review-run-select-menu')).getByLabelText('Review in progress')).toBeInTheDocument();
    expect(within(screen.getByTestId('review-run-select-menu')).getByLabelText('Review complete')).toBeInTheDocument();
  });

  it('disables lowest-confidence order for the selected field only', () => {
    const changedOrder = vi.fn();
    render(
      <ReviewRunControls
        runs={[run]}
        selectedRunId="9"
        onSelectedRunChange={vi.fn()}
        selectedFieldId="5"
        onSelectedFieldChange={vi.fn()}
        order="shuffle"
        onOrderChange={changedOrder}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Review order' }));
    const confidence = screen.getByRole('button', { name: 'Lowest confidence' });
    expect(confidence).toBeDisabled();
    expect(confidence).toHaveAttribute('title', expect.stringContaining('no confidence values'));
    fireEvent.click(confidence);
    expect(changedOrder).not.toHaveBeenCalled();
  });

  it('offers stable run order even when confidence ordering is unavailable', () => {
    const changedOrder = vi.fn();
    render(
      <ReviewRunControls
        runs={[run]}
        selectedRunId="9"
        onSelectedRunChange={vi.fn()}
        selectedFieldId="5"
        onSelectedFieldChange={vi.fn()}
        order="shuffle"
        onOrderChange={changedOrder}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Review order' }));
    expect(screen.getByRole('button', { name: 'Lowest confidence' })).toBeDisabled();
    const rowOrder = screen.getByRole('button', { name: 'Row order' });
    expect(rowOrder).toBeEnabled();
    fireEvent.click(rowOrder);
    expect(changedOrder).toHaveBeenCalledWith('row');
  });

  it('keeps ordering in a compact popover and exposes results separately', () => {
    const changedOrder = vi.fn();
    render(
      <ReviewRunControls
        runs={[run]}
        selectedRunId="9"
        onSelectedRunChange={vi.fn()}
        selectedFieldId={null}
        onSelectedFieldChange={vi.fn()}
        order="shuffle"
        onOrderChange={changedOrder}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Review order' }));
    expect(screen.getByTestId('review-order')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Random' })).toHaveAttribute(
      'title',
      expect.stringContaining('Back keeps rows already shown'),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Lowest confidence' }));
    expect(changedOrder).toHaveBeenCalledWith('confidence');
    fireEvent.click(screen.getByRole('button', { name: 'Results' }));
    expect(screen.getByTestId('review-run-summary')).toHaveTextContent('5 reviewed of 10');
    expect(screen.getByTestId('review-run-summary')).toHaveTextContent('75% correct among reviewed (3/4)');
    fireEvent.click(screen.getByRole('button', { name: 'Close review results' }));
    expect(screen.queryByTestId('review-results')).not.toBeInTheDocument();
  });

  it('does not show accuracy without explicit outcome facts', () => {
    render(<ReviewRunSummary run={{ ...run, total: { ...run.total, reviewedCount: 0, acceptedCount: 0, incorrectCount: 0 } }} />);
    expect(screen.getByTestId('review-run-summary')).toHaveTextContent('0 reviewed of 10');
    expect(screen.getByText('No graded decisions yet')).toBeInTheDocument();
    expect(screen.queryByText(/accuracy \(/)).not.toBeInTheDocument();
  });

  it('offers the durable completion action', () => {
    const changeStatus = vi.fn();
    render(
      <ReviewRunControls
        runs={[run]}
        selectedRunId="9"
        onSelectedRunChange={vi.fn()}
        selectedFieldId={null}
        onSelectedFieldChange={vi.fn()}
        order="shuffle"
        onOrderChange={vi.fn()}
        onRunStatusChange={changeStatus}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Mark review complete' }));
    expect(changeStatus).toHaveBeenCalledWith(run, 'complete');
  });

  it('keeps injected row controls and optional older-run paging in the toolbar', () => {
    const loadMore = vi.fn();
    render(
      <ReviewRunControls
        runs={[run]}
        selectedRunId="9"
        onSelectedRunChange={vi.fn()}
        selectedFieldId={null}
        onSelectedFieldChange={vi.fn()}
        order="shuffle"
        onOrderChange={vi.fn()}
        hasMore
        onLoadMore={loadMore}
      >
        <span>Row 7 · 1 of 4</span>
      </ReviewRunControls>,
    );

    expect(screen.getByText('Row 7 · 1 of 4')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Older runs' }));
    expect(loadMore).toHaveBeenCalledOnce();
  });

  it('disables scope, details, paging, and completion controls together', () => {
    render(
      <ReviewRunControls
        runs={[run]}
        selectedRunId="9"
        onSelectedRunChange={vi.fn()}
        selectedFieldId={null}
        onSelectedFieldChange={vi.fn()}
        order="shuffle"
        onOrderChange={vi.fn()}
        onRunStatusChange={vi.fn()}
        hasMore
        onLoadMore={vi.fn()}
        disabled
      />,
    );

    expect(screen.getByTestId('review-run-select')).toBeDisabled();
    expect(screen.getByTestId('review-field-select')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Review order' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Results' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Older runs' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Mark review complete' })).toBeDisabled();
  });
});
