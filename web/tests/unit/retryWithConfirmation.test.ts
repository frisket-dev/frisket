import { expect, it, vi } from 'vitest';
import { retryWithConfirmation, retryFailedRowsWithConfirmation } from '../../src/api/retryWithConfirmation';
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

function failedRow(id: string, runId: string | null) {
  return {
    id, index: Number(id), cells: { '4': null },
    provenance: {
      '4': {
        currentValueRef: { kind: 'run_result' as const, runId, rowId: id, columnId: '4', opId: null },
        runId: runId ?? '', model: '', actionName: '', cost: 0, confidence: null, justification: '',
      },
    },
  };
}

it('retries mixed execution heads across pages, with separate normal confirmations', async () => {
  const getSheetData = vi.fn()
    .mockResolvedValueOnce({ rows: [failedRow('1', '11'), failedRow('2', '22')], total: 3 })
    .mockResolvedValueOnce({ rows: [failedRow('3', '11')], total: 3 });
  const runAction = vi.fn()
    .mockRejectedValueOnce(new ConfirmationRequiredError({ cost: 3, rows: 2, promise_set_hash: 'first-group' }, 'Retry estimate', 'model_cost'))
    .mockResolvedValueOnce({ runId: '33' })
    .mockRejectedValueOnce(new ConfirmationRequiredError({ cost: 2, rows: 1, promise_set_hash: 'second-group' }, 'Retry estimate', 'model_cost'))
    .mockResolvedValueOnce({ runId: '44' });
  const confirm = vi.fn().mockResolvedValue(true);
  const afterRetry = vi.fn();
  await retryFailedRowsWithConfirmation({ getSheetData, runAction }, confirm, '7', { id: '4', name: 'Answer' }, 'model_error', afterRetry);
  expect(getSheetData).toHaveBeenNthCalledWith(1, '7', 0, 1000, { filter: { Answer: { failed: 'model_error' } }, columnIds: ['4'], valueMode: 'bounded' });
  expect(getSheetData.mock.calls[1][1]).toBe(2);
  expect(runAction.mock.calls[0][0]).toMatchObject({ params: { source_run_id: 11 }, scope: { row_ids: [1, 3] } });
  expect(runAction.mock.calls[2][0]).toMatchObject({ params: { source_run_id: 22 }, scope: { row_ids: [2] } });
  expect(runAction.mock.calls[1][0]).toEqual({ ...runAction.mock.calls[0][0], confirmation: 'first-group' });
  expect(runAction.mock.calls[3][0]).toEqual({ ...runAction.mock.calls[2][0], confirmation: 'second-group' });
  expect(afterRetry.mock.calls).toEqual([['33'], ['44']]);
});

it('stops all further retry groups when approval is cancelled', async () => {
  const getSheetData = vi.fn().mockResolvedValue({ rows: [failedRow('1', '11'), failedRow('2', '22')], total: 2 });
  const runAction = vi.fn().mockRejectedValue(new ConfirmationRequiredError({ cost: 3, rows: 1, promise_set_hash: 'first' }, 'Retry estimate', 'model_cost'));
  const afterRetry = vi.fn();
  await retryFailedRowsWithConfirmation({ getSheetData, runAction }, vi.fn().mockResolvedValue(false), '7', { id: '4', name: 'Answer' }, 'model_error', afterRetry);
  expect(runAction).toHaveBeenCalledTimes(1);
  expect(afterRetry).not.toHaveBeenCalled();
});

it('refuses missing head provenance before buying any group', async () => {
  const getSheetData = vi.fn().mockResolvedValue({ rows: [failedRow('1', '11'), failedRow('2', null)], total: 2 });
  const runAction = vi.fn();
  await expect(retryFailedRowsWithConfirmation({ getSheetData, runAction }, vi.fn(), '7', { id: '4', name: 'Answer' }, 'model_error', vi.fn())).rejects.toThrow('no saved execution');
  expect(runAction).not.toHaveBeenCalled();
});
