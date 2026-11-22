// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, renderHook } from '@testing-library/react';
import { ApiError, ConfirmationRequiredError, type RunEstimate } from '../../src/api/open';
import {
  runWithCostConfirmationRetry,
  useCostConfirmationRetry,
} from '../../src/hooks/useCostConfirmationRetry';

afterEach(cleanup);

function estimate(hash?: string): RunEstimate {
  return {
    cost: 0.12,
    billed_cost: 120_000,
    policy_id: 'frisket.pricing.identity.v1',
    rows: 10,
    ...(hash === undefined ? {} : { promise_set_hash: hash }),
  };
}

describe('runWithCostConfirmationRetry', () => {
  it('returns the completed value immediately when no 402 is thrown', async () => {
    const requestCostConfirmation = vi.fn();
    const run = vi.fn().mockResolvedValue('ok');

    const outcome = await runWithCostConfirmationRetry(run, requestCostConfirmation);

    expect(outcome).toEqual({ status: 'completed', value: 'ok' });
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith(undefined);
    expect(requestCostConfirmation).not.toHaveBeenCalled();
  });

  it('prompts on 402 and retries across chained confirmation hashes', async () => {
    const first = estimate('a'.repeat(64));
    const second = estimate('b'.repeat(64));
    const requestCostConfirmation = vi.fn()
      .mockResolvedValueOnce(true)
      .mockResolvedValueOnce(true);
    const run = vi.fn()
      .mockRejectedValueOnce(new ConfirmationRequiredError(first, 'Confirm first quote.'))
      .mockRejectedValueOnce(new ConfirmationRequiredError(second, 'Confirm updated quote.'))
      .mockResolvedValueOnce({ jobId: 42 });

    const outcome = await runWithCostConfirmationRetry(run, requestCostConfirmation);

    expect(outcome).toEqual({ status: 'completed', value: { jobId: 42 } });
    expect(run).toHaveBeenNthCalledWith(1, undefined);
    expect(run).toHaveBeenNthCalledWith(2, 'a'.repeat(64));
    expect(run).toHaveBeenNthCalledWith(3, 'b'.repeat(64));
    expect(requestCostConfirmation).toHaveBeenNthCalledWith(1, first, 'Confirm first quote.');
    expect(requestCostConfirmation).toHaveBeenNthCalledWith(2, second, 'Confirm updated quote.');
  });

  it('returns cancelled without retrying when the user declines confirmation', async () => {
    const requestCostConfirmation = vi.fn().mockResolvedValue(false);
    const run = vi.fn().mockRejectedValueOnce(
      new ConfirmationRequiredError(estimate('c'.repeat(64)), 'Confirm refresh.'),
    );

    const outcome = await runWithCostConfirmationRetry(run, requestCostConfirmation);

    expect(outcome).toEqual({ status: 'cancelled' });
    expect(run).toHaveBeenCalledTimes(1);
    expect(requestCostConfirmation).toHaveBeenCalledTimes(1);
  });

  it('rethrows non-confirmation errors, hashless challenges, and missing requesters', async () => {
    const requestCostConfirmation = vi.fn();

    await expect(
      runWithCostConfirmationRetry(
        vi.fn().mockRejectedValueOnce(new ApiError(400, 'Bad input', 'invalid_request')),
        requestCostConfirmation,
      ),
    ).rejects.toThrow('Bad input');

    await expect(
      runWithCostConfirmationRetry(
        vi.fn().mockRejectedValueOnce(new ConfirmationRequiredError(estimate(), 'Missing hash.')),
        requestCostConfirmation,
      ),
    ).rejects.toThrow('Missing hash.');

    await expect(
      runWithCostConfirmationRetry(
        vi.fn().mockRejectedValueOnce(
          new ConfirmationRequiredError(estimate('d'.repeat(64)), 'No requester.'),
        ),
        undefined,
      ),
    ).rejects.toThrow('No requester.');

    expect(requestCostConfirmation).not.toHaveBeenCalled();
  });

  it('stops cleanly when shouldContinue becomes false before or during confirmation', async () => {
    let active = false;
    const requestCostConfirmation = vi.fn().mockResolvedValue(true);
    const runBefore = vi.fn().mockRejectedValueOnce(
      new ConfirmationRequiredError(estimate('e'.repeat(64)), 'Stale before prompt.'),
    );

    const beforeOutcome = await runWithCostConfirmationRetry(
      runBefore,
      requestCostConfirmation,
      { shouldContinue: () => active },
    );
    expect(beforeOutcome).toEqual({ status: 'cancelled' });
    expect(requestCostConfirmation).not.toHaveBeenCalled();

    active = true;
    const runDuring = vi.fn().mockRejectedValueOnce(
      new ConfirmationRequiredError(estimate('f'.repeat(64)), 'Stale during prompt.'),
    );
    const duringRequester = vi.fn(async () => {
      active = false;
      return true;
    });

    const duringOutcome = await runWithCostConfirmationRetry(
      runDuring,
      duringRequester,
      { shouldContinue: () => active },
    );
    expect(duringOutcome).toEqual({ status: 'cancelled' });
    expect(runDuring).toHaveBeenCalledTimes(1);
    expect(duringRequester).toHaveBeenCalledTimes(1);
  });
});

describe('useCostConfirmationRetry', () => {
  it('binds the runner to the provided confirmation requester', async () => {
    const requestCostConfirmation = vi.fn().mockResolvedValue(true);
    const { result } = renderHook(() => useCostConfirmationRetry(requestCostConfirmation));
    const run = vi.fn()
      .mockRejectedValueOnce(
        new ConfirmationRequiredError(estimate('1'.repeat(64)), 'Confirm via hook.'),
      )
      .mockResolvedValueOnce('confirmed');

    const outcome = await result.current(run);

    expect(outcome).toEqual({ status: 'completed', value: 'confirmed' });
    expect(run).toHaveBeenNthCalledWith(2, '1'.repeat(64));
  });
});
