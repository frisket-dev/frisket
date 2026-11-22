import { useCallback } from 'react';
import { ConfirmationRequiredError } from '../api/open';
import type { CostConfirmationRequester } from '../api/backfillWithConfirmation';

export type CostConfirmationRetryOutcome<T> =
  | { status: 'completed'; value: T }
  | { status: 'cancelled' };

export interface CostConfirmationRetryOptions {
  /** Guard evaluated before prompting and before retrying after confirmation. */
  shouldContinue?: () => boolean;
}

export type CostConfirmationRetryRunner = <T>(
  run: (confirmation?: string) => Promise<T>,
  options?: CostConfirmationRetryOptions,
) => Promise<CostConfirmationRetryOutcome<T>>;

/**
 * Execute an HTTP 402 confirmation-gated operation, prompting via
 * `requestCostConfirmation` and echoing each server-issued `promise_set_hash`
 * until the request completes, the user cancels, or `shouldContinue` returns
 * false. Non-confirmation errors, hashless challenges, and challenges with no
 * requester propagate to the caller.
 */
export async function runWithCostConfirmationRetry<T>(
  run: (confirmation?: string) => Promise<T>,
  requestCostConfirmation?: CostConfirmationRequester,
  options?: CostConfirmationRetryOptions,
): Promise<CostConfirmationRetryOutcome<T>> {
  const shouldContinue = options?.shouldContinue;
  let confirmation: string | undefined;
  for (;;) {
    try {
      const value = await run(confirmation);
      return { status: 'completed', value };
    } catch (caught) {
      if (
        !(caught instanceof ConfirmationRequiredError)
        || !caught.estimate.promise_set_hash
        || !requestCostConfirmation
      ) {
        throw caught;
      }
      if (shouldContinue && !shouldContinue()) {
        return { status: 'cancelled' };
      }
      const confirmed = await requestCostConfirmation(
        caught.estimate,
        caught.message,
      );
      if (!confirmed || (shouldContinue && !shouldContinue())) {
        return { status: 'cancelled' };
      }
      confirmation = caught.estimate.promise_set_hash;
    }
  }
}

export function useCostConfirmationRetry(
  requestCostConfirmation?: CostConfirmationRequester,
): CostConfirmationRetryRunner {
  return useCallback(
    <T,>(
      run: (confirmation?: string) => Promise<T>,
      options?: CostConfirmationRetryOptions,
    ) => runWithCostConfirmationRetry(run, requestCostConfirmation, options),
    [requestCostConfirmation],
  );
}
