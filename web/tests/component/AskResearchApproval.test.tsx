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
        kind: 'budget', budget_usd: '5.00', spent_usd: '3.25', committed_usd: '1.50', next_estimate_usd: '1.00',
      }}
      onContinue={onContinue}
      onStop={vi.fn()}
    />);

    expect(screen.getByText('Spent $3.25 · committed $1.50 · next action about $1.00')).toBeVisible();
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
      approval={{ kind: 'turn_limit', turns_used: 8, max_turns: 8 }}
      onContinue={onContinue}
      onStop={vi.fn()}
    />);

    const turns = screen.getByLabelText('New max turns');
    await userEvent.clear(turns);
    await userEvent.type(turns, '12');
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(onContinue).toHaveBeenLastCalledWith({ max_turns: 12 });

    rerender(<AskResearchApproval
      approval={{ kind: 'turn_limit', turns_used: 8, max_turns: 8 }}
      onContinue={onContinue}
      onStop={vi.fn()}
    />);
    await userEvent.click(screen.getByRole('checkbox', { name: 'No limit' }));
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(onContinue).toHaveBeenLastCalledWith({ max_turns: null });
    expect(screen.getByRole('button', { name: 'Stop' })).toBeVisible();
  });
});
