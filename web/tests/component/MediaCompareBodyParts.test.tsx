// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  CompareColumnsState,
  CompareFooter,
  CompareResultState,
  CompareVoteControl,
} from '../../src/workbench/mediaCompareBodyParts';

afterEach(cleanup);

describe('shared media comparison presentation', () => {
  it('shows one truthful prompt for zero, unavailable, and ready variants', () => {
    const view = render(<CompareColumnsState
      testIdPrefix="fixture"
      configuredCount={0}
      runnableCount={0}
      showReady
    />);
    expect(screen.getByTestId('fixture-empty-variants')).toHaveTextContent(
      'No variants — add an engine to compare.',
    );
    expect(screen.queryByTestId('fixture-run-hint')).not.toBeInTheDocument();

    view.rerender(<CompareColumnsState
      testIdPrefix="fixture"
      configuredCount={2}
      runnableCount={0}
      showReady
    />);
    expect(screen.getByTestId('fixture-empty-variants')).toHaveTextContent(
      'No engine is ready — configure a variant to finish setup.',
    );
    expect(screen.queryByText(/No variants/)).not.toBeInTheDocument();
    expect(screen.queryByTestId('fixture-run-hint')).not.toBeInTheDocument();

    view.rerender(<CompareColumnsState
      testIdPrefix="fixture"
      configuredCount={2}
      runnableCount={2}
      showReady
    />);
    expect(screen.queryByTestId('fixture-empty-variants')).not.toBeInTheDocument();
    expect(screen.getByTestId('fixture-run-hint')).toHaveTextContent(
      'Ready to compare — select Run comparison.',
    );
  });

  it('keeps the shared vote control semantic and clickable', async () => {
    const onVote = vi.fn();
    render(<CompareVoteControl
      testId="fixture-vote"
      engineId="fixture-engine"
      vote="keep"
      label="Fixture engine"
      summary="detailed"
      onVote={onVote}
    />);
    const vote = screen.getByTestId('fixture-vote');
    expect(vote).toHaveAttribute('data-vote', 'keep');
    expect(vote).toHaveTextContent('✓Fixture enginedetailed');
    await userEvent.click(vote);
    expect(onVote).toHaveBeenCalledOnce();
  });

  it('shares pending, running, failure with Retry, and completed content', async () => {
    const onRetry = vi.fn();
    const renderResult = vi.fn(() => <p>completed domain content</p>);
    const props = {
      engineLabel: 'Fixture engine',
      pendingLabel: 'not run yet',
      runningLabel: 'working…',
      testIdPrefix: 'fixture-engine',
      onRetry,
      renderResult,
    };
    const view = render(<CompareResultState run={undefined} {...props} />);
    expect(screen.getByText('not run yet')).toBeVisible();
    expect(renderResult).not.toHaveBeenCalled();

    view.rerender(<CompareResultState run={{ status: 'running' }} {...props} />);
    expect(screen.getByText('working…')).toBeVisible();
    expect(renderResult).not.toHaveBeenCalled();

    view.rerender(<CompareResultState
      run={{ status: 'error', message: 'fixture failed safely' }}
      {...props}
    />);
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Fixture engine failedfixture failed safely',
    );
    expect(renderResult).not.toHaveBeenCalled();
    await userEvent.click(screen.getByTestId('fixture-engine-retry'));
    expect(onRetry).toHaveBeenCalledOnce();

    const completed = { status: 'done' as const, results: 'done', confidence: null };
    view.rerender(<CompareResultState run={completed} {...props} />);
    expect(screen.getByText('completed domain content')).toBeVisible();
    expect(renderResult).toHaveBeenCalledOnce();
    expect(renderResult).toHaveBeenCalledWith(completed);
  });

  it('shares accessible footer navigation and fallback copy', async () => {
    const previous = vi.fn();
    const next = vi.fn();
    const view = render(<CompareFooter
      footerTestId="fixture-footer"
      navigationTestId="fixture-navigation"
      verdictTestId="fixture-verdict"
      verdict="Fixture ✓1"
      difference={{
        label: '2 positions differ',
        previousLabel: 'Previous position',
        nextLabel: 'Next position',
        onPrevious: previous,
        onNext: next,
      }}
      fallback="waiting"
    />);
    await userEvent.click(screen.getByRole('button', { name: 'Previous position' }));
    await userEvent.click(screen.getByRole('button', { name: 'Next position' }));
    expect(previous).toHaveBeenCalledOnce();
    expect(next).toHaveBeenCalledOnce();
    expect(screen.getByTestId('fixture-verdict')).toHaveTextContent('Verdict so far: Fixture ✓1');

    view.rerender(<CompareFooter
      footerTestId="fixture-footer"
      navigationTestId="fixture-navigation"
      verdictTestId="fixture-verdict"
      verdict="No votes cast yet"
      difference={null}
      fallback="waiting"
    />);
    expect(screen.queryByRole('button', { name: 'Previous position' })).not.toBeInTheDocument();
    expect(screen.getByTestId('fixture-navigation')).toHaveTextContent('waiting');
  });
});
