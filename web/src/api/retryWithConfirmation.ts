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

/** Retry the executions that actually produced the selected column's failures. */
export async function retryFailedRowsWithConfirmation(
  api: Pick<ProjectApiPort, 'runAction' | 'getSheetData'>,
  requestConfirmation: CostConfirmationRequester,
  sheetId: string,
  column: { id: string; name: string },
  outcome: string,
  afterRetry: (runId: string) => void,
): Promise<boolean> {
  const groups = new Map<string, number[]>();
  for (let offset = 0; ; ) {
    const page = await api.getSheetData(sheetId, offset, 1000, {
      selection: {
        scope: { kind: 'sheet', sheet_id: Number(sheetId) },
        filters: [{ column_id: Number(column.id), condition: { failed: outcome } }],
        order: [], limit: null,
      },
      columnIds: [column.id], valueMode: 'bounded',
    });
    for (const row of page.rows) {
      const ref = row.provenance[column.id]?.currentValueRef;
      if (ref?.kind !== 'run_result' || !ref.runId) {
        throw new Error('A failed row has no saved execution available to retry.');
      }
      const rowIds = groups.get(ref.runId) ?? [];
      rowIds.push(Number(row.id));
      groups.set(ref.runId, rowIds);
    }
    offset += page.rows.length;
    if (page.rows.length === 0 || offset >= page.total) break;
  }
  let accepted = false;
  for (const [sourceRunId, rowIds] of groups) {
    const result = await retryWithConfirmation(api, requestConfirmation, sheetId, sourceRunId, rowIds);
    if (result === null) return accepted;
    if (result.runId) {
      accepted = true;
      afterRetry(result.runId);
    }
  }
  return accepted;
}
