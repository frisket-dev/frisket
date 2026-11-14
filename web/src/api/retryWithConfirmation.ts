import { ConfirmationRequiredError, freshActionRequestKey } from './open';
import type { ProjectApiPort } from './ports';
import type { CostConfirmationRequester } from './backfillWithConfirmation';

/** Explicit retry retains the original execution and gets a fresh approval decision. */
export async function retryWithConfirmation(
  api: Pick<ProjectApiPort, 'runAction'>,
  requestConfirmation: CostConfirmationRequester,
  sheetId: string,
  sourceRunId: string,
  rowIds?: number[],
) {
  const request = {
    action_id: 'run.retry' as const,
    idempotency_key: freshActionRequestKey('run.retry'),
    scope: { kind: 'sheet_rows' as const, sheet_id: Number(sheetId),
      ...(rowIds === undefined ? {} : { row_ids: rowIds }) },
    params: { source_run_id: Number(sourceRunId) },
    output_names: {},
  };
  try {
    return await api.runAction(request);
  } catch (error) {
    if (!(error instanceof ConfirmationRequiredError) || !error.estimate.promise_set_hash) throw error;
    if (!await requestConfirmation(error.estimate, error.message)) return null;
    return api.runAction({ ...request, confirmation: error.estimate.promise_set_hash });
  }
}
