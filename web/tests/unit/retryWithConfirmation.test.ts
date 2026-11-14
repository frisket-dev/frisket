import { expect, it, vi } from 'vitest';
import { retryWithConfirmation } from '../../src/api/retryWithConfirmation';
import { ConfirmationRequiredError } from '../../src/api/open';

it('retries only the selected original work with the same request identity through approval', async () => {
  const runAction = vi.fn()
    .mockRejectedValueOnce(new ConfirmationRequiredError({ cost: 3, rows: 1, promise_set_hash: 'approved-work' }, 'Retry estimate', 'model_cost'))
    .mockResolvedValueOnce({ runId: '22' });
  const confirm = vi.fn().mockResolvedValue(true);
  expect(await retryWithConfirmation({ runAction }, confirm, '7', '11', [3])).toEqual({ runId: '22' });
  const initial = runAction.mock.calls[0][0];
  expect(initial).toMatchObject({ action_id: 'run.retry', scope: { sheet_id: 7, row_ids: [3] }, params: { source_run_id: 11 } });
  expect(runAction.mock.calls[1][0]).toEqual({ ...initial, confirmation: 'approved-work' });
});

it('cancelling retry approval makes no second submission', async () => {
  const runAction = vi.fn().mockRejectedValue(new ConfirmationRequiredError({ cost: 3, rows: 1, promise_set_hash: 'work' }, 'Retry estimate', 'model_cost'));
  expect(await retryWithConfirmation({ runAction }, vi.fn().mockResolvedValue(false), '7', '11')).toBeNull();
  expect(runAction).toHaveBeenCalledTimes(1);
});
