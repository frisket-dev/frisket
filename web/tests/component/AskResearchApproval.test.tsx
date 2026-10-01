// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AskResearchApproval } from '../../src/components/project-ask/AskResearchApproval';

afterEach(cleanup);

describe('AskResearchApproval', () => {
  it('shows the proposed action and lets the user continue, skip, or stop', async () => {
    const onContinue = vi.fn();
    const onSkip = vi.fn();
    const onStop = vi.fn();
    render(<AskResearchApproval
      approval={{
        id: 'approval-action',
        kind: 'action',
        title: 'Extract organizations',
        effect: 'Writes a new Organization column to 42 rows.',
        targets: ['Interviews', 'Organization'],
        estimate_usd: '0.18',
        can_skip: true,
      }}
      onContinue={onContinue}
      onSkip={onSkip}
      onStop={onStop}
    />);

    expect(screen.getByRole('heading', { name: 'Extract organizations' })).toBeVisible();
    expect(screen.getByText('Writes a new Organization column to 42 rows.')).toBeVisible();
    expect(screen.getByText('Interviews · Organization')).toBeVisible();
    expect(screen.getByText('Estimated cost $0.18')).toBeVisible();

    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await userEvent.click(screen.getByRole('button', { name: 'Skip' }));
    await userEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(onContinue).toHaveBeenCalledWith(undefined);
    expect(onSkip).toHaveBeenCalledOnce();
    expect(onStop).toHaveBeenCalledOnce();
  });

  it('requires a higher total before continuing a budget-paused run', async () => {
    const onContinue = vi.fn();
    render(<AskResearchApproval
      approval={{
        id: 'approval-budget', kind: 'budget', budget_micros: 5_000_000, settled_micros: 3_250_000,
        reserved_micros: 1_500_000, next_estimate_micros: 1_000_000, currency: 'USD',
      }}
      onContinue={onContinue}
      onStop={vi.fn()}
    />);

    expect(screen.getByText('Spent $3.25 · reserved $1.50 · next action about $1.00')).toBeVisible();
    const continueButton = screen.getByRole('button', { name: 'Continue' });
    expect(continueButton).toBeDisabled();
    const input = screen.getByLabelText('New total budget (USD)');
    await userEvent.clear(input);
    await userEvent.type(input, '7.50');
    expect(continueButton).toBeEnabled();
    await userEvent.click(continueButton);
    expect(onContinue).toHaveBeenCalledWith({ budget_usd: '7.50' });
    expect(screen.getByRole('button', { name: 'Stop' })).toBeVisible();
  });

  it('raises or removes a reached turn limit while keeping Stop available', async () => {
    const onContinue = vi.fn();
    const { rerender } = render(<AskResearchApproval
      approval={{ id: 'approval-turns', kind: 'turn_limit', turns_used: 8, max_turns: 8 }}
      onContinue={onContinue}
      onStop={vi.fn()}
    />);

    const turns = screen.getByLabelText('New max turns');
    await userEvent.clear(turns);
    await userEvent.type(turns, '12');
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(onContinue).toHaveBeenLastCalledWith({ max_turns: 12 });

    rerender(<AskResearchApproval
      approval={{ id: 'approval-turns', kind: 'turn_limit', turns_used: 8, max_turns: 8 }}
      onContinue={onContinue}
      onStop={vi.fn()}
    />);
    await userEvent.click(screen.getByRole('checkbox', { name: 'No limit' }));
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(onContinue).toHaveBeenLastCalledWith({ max_turns: null });
    expect(screen.getByRole('button', { name: 'Stop' })).toBeVisible();
  });

  it('offers only Stop when the provider cannot quote the next operation', async () => {
    const onStop = vi.fn();
    render(<AskResearchApproval
      approval={{ id: 'approval-unknown', kind: 'unknown_cost', operation: 'search' }}
      onContinue={vi.fn()}
      onStop={onStop}
    />);

    expect(screen.getByRole('heading', { name: 'Cost unavailable' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Skip' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(onStop).toHaveBeenCalledOnce();
  });

  it('can retry after a manager restores a disabled skill', async () => {
    const onContinue = vi.fn();
    render(<AskResearchApproval
      approval={{ id: 'approval-skills', kind: 'skills', message: 'The selected skill is disabled.' }}
      onContinue={onContinue}
      onStop={vi.fn()}
    />);

    expect(screen.getByText('The selected skill is disabled.')).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(onContinue).toHaveBeenCalledWith(undefined);
    expect(screen.getByRole('button', { name: 'Stop' })).toBeVisible();
  });
});
