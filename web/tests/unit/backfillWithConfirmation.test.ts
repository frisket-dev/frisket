// backfill-cost-gate-v1 (frontend): the shared run.backfill cost-gate loop.
//
// Proves the three behaviors both callers (runActionBackfill + ColumnDrawer)
// inherit from this helper:
//  - under the gate ($0/deterministic): NO prompt, one backfillColumn call.
//  - over the gate: 402 -> priced prompt -> confirm -> resubmit with
//    confirmed:true, and the BackfillResult flows back.
//  - cancel: resolves null (caller aborts cleanly), NO confirmed resubmit.

import { afterEach, describe, expect, it, vi } from 'vitest';
import { backfillWithConfirmation } from '../../src/api/backfillWithConfirmation';
import { ConfirmationRequiredError } from '../../src/api/open';
import type { BackfillResult, RunEstimate } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';

const api = createProjectApi('test-project');

afterEach(() => {
  vi.restoreAllMocks();
});

const result = (over: Partial<BackfillResult> = {}): BackfillResult => ({
  filled: 1,
  runId: 'run-backfill-1',
  ...over,
});

describe('backfillWithConfirmation', () => {
  it('under the gate: runs with no prompt and returns the result', async () => {
    const spy = vi.spyOn(api, 'backfillColumn').mockResolvedValue(result());
    const requestConfirmation = vi.fn(async () => true);

    const out = await backfillWithConfirmation(api, requestConfirmation, 'sheet-1', 'extracted');

    expect(out).toEqual(result());
    expect(requestConfirmation).not.toHaveBeenCalled();
    expect(spy).toHaveBeenCalledTimes(1);
    // The transport emits no confirmation token on the common under-gate path.
    expect(spy).toHaveBeenCalledWith('sheet-1', 'extracted', false, undefined);
  });

  it('over the gate: prompts with the priced estimate, then resubmits confirmed', async () => {
    const estimate: RunEstimate = {
      cost: 4.2,
      rows: 900,
      promise_set_hash: 'backfill-promise-set-v1',
    };
    const spy = vi
      .spyOn(api, 'backfillColumn')
      .mockRejectedValueOnce(new ConfirmationRequiredError(estimate, 'Backfill will cost $4.20', 'model_cost'))
      .mockResolvedValueOnce(result({ filled: 900, runId: 'run-backfill-2' }));
    const requestConfirmation = vi.fn(async () => true);

    const out = await backfillWithConfirmation(api, requestConfirmation, 'sheet-1', 'extracted');

    expect(requestConfirmation).toHaveBeenCalledWith(estimate, 'Backfill will cost $4.20');
    expect(out).toEqual(result({ filled: 900, runId: 'run-backfill-2' }));
    expect(spy).toHaveBeenCalledTimes(2);
    // First attempt unconfirmed, retry confirmed.
    expect(spy).toHaveBeenNthCalledWith(1, 'sheet-1', 'extracted', false, undefined);
    expect(spy).toHaveBeenNthCalledWith(
      2,
      'sheet-1',
      'extracted',
      true,
      undefined,
      'backfill-promise-set-v1',
    );
  });

  it('explicit rowIds (deliberate per-row retry) ride both the attempt and the confirmed resubmit', async () => {
    const spy = vi
      .spyOn(api, 'backfillColumn')
      .mockRejectedValueOnce(new ConfirmationRequiredError({ cost: 1.5, rows: 1, promise_set_hash: 'row-retry-quote' }, 'Retry will cost $1.50', 'model_cost'))
      .mockResolvedValueOnce(result({ filled: 0, runId: 'run-retry-1' }));
    const requestConfirmation = vi.fn(async () => true);

    const out = await backfillWithConfirmation(api, requestConfirmation, 'sheet-1', 'translated', [7]);

    expect(out).toEqual(result({ filled: 0, runId: 'run-retry-1' }));
    expect(spy).toHaveBeenNthCalledWith(1, 'sheet-1', 'translated', false, [7]);
    expect(spy).toHaveBeenNthCalledWith(2, 'sheet-1', 'translated', true, [7], 'row-retry-quote');
  });

  it('cancel: resolves null and never resubmits (no mutation)', async () => {
    const spy = vi
      .spyOn(api, 'backfillColumn')
      .mockRejectedValueOnce(new ConfirmationRequiredError({ cost: 9, rows: 100 }, 'pricey', 'model_cost'));
    const requestConfirmation = vi.fn(async () => false);

    const out = await backfillWithConfirmation(api, requestConfirmation, 'sheet-1', 'extracted');

    expect(out).toBeNull();
    expect(requestConfirmation).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledTimes(1); // no confirmed resubmit
  });

  it('a non-confirmation failure propagates unchanged', async () => {
    const boom = new Error('backend exploded');
    vi.spyOn(api, 'backfillColumn').mockRejectedValueOnce(boom);
    const requestConfirmation = vi.fn(async () => true);

    await expect(
      backfillWithConfirmation(api, requestConfirmation, 'sheet-1', 'extracted'),
    ).rejects.toThrow('backend exploded');
    expect(requestConfirmation).not.toHaveBeenCalled();
  });
});
