import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  cancelRunContract,
  getActionJobContract,
  getReceiptContract,
  getRunProgressContract,
  getRunRowsContract,
  listActionJobsContract,
} from '../../src/api/httpContractRoutes';
import { ApiError } from '../../src/api/contractErrors';
import { jsonResponse } from '../support/httpContractTestHelpers';

async function expectApiError(
  request: Promise<unknown>,
  status: number,
  message: string,
): Promise<void> {
  await expect(request).rejects.toBeInstanceOf(ApiError);
  await expect(request).rejects.toMatchObject({
    name: 'ApiError',
    status,
    message,
    code: undefined,
    details: undefined,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('run and receipt HTTP contracts', () => {
  it('maps the server preparation hint for a running zero-row action run', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      schema_version: 'frisket.actions.v1', action: 'run_status', project_id: 'project-1',
      run: {
        id: 17, sheet_id: 2, action_kind: 'map.translate', action_name: 'Translate',
        status: 'running', total_rows: 10, completed_rows: 0, failed_rows: 0,
        cost_actual: null, cost_estimate: null,
        public_status: {
          run_id: 17, action_kind: 'map.translate', action_name: 'Translate', status: 'running',
          live: true, cancel_requested: true, completed: 0, total: 10, failed: 0, cost: null,
          attempted: 0, succeeded: 0, skipped: 0, remaining: 10,
          timing: {},
          preparation: {
            message: 'Preparing the language model if this worker needs it, then translating…',
          },
        },
      },
    })));

    await expect(getRunProgressContract('project-1', 17)).resolves.toMatchObject({
      status: 'running',
      completedRows: 0,
      attemptedRows: 0,
      succeededRows: 0,
      skippedRows: 0,
      remainingRows: 10,
      cancelRequested: true,
      preparation: {
        message: 'Preparing the language model if this worker needs it, then translating…',
      },
    });
  });

  it('accepts a durable cancel request while the live writer is stopping', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      run_id: 17,
      status: 'cancel_pending',
      cancel_requested: true,
      completed: 3,
      total: 10,
      queue_job_id: 23,
      queue_cancelled: false,
      reason: 'live_writer_must_terminalize',
    }, 202)));

    await expect(cancelRunContract('project-1', 17)).resolves.toBeUndefined();
  });

  it.each([null, false, 0, '', { matched: 2 }])('maps receipt presentation and domain value %j', async (value) => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      schema_version: 'frisket.receipt.v1',
      receipt_id: 'receipt-1',
      project_id: 'project-1',
      action_id: 'action-1',
      action_kind: 'map.template',
      run_id: 7,
      change_ids: [3],
      idempotency_key: 'template@sha256:v1',
      params_hash: 'sha256:v1',
      status: 'completed',
      inputs: [],
      outputs: [],
      value,
      provider_use: [],
      records: [],
      errors: [],
    })));

    const receipt = await getReceiptContract('project-1', 'receipt-1');
    expect(receipt).toMatchObject({
      receiptId: 'receipt-1',
      actionId: 'action-1',
      actionKind: 'map.template',
      runId: '7',
      value,
    });
    expect(Object.keys(receipt).sort()).toEqual([
      'actionId', 'actionKind', 'changeIds', 'errors', 'idempotencyKey', 'inputs',
      'outputs', 'paramsHash', 'projectId', 'providerUse', 'receiptId',
      'records', 'runId', 'schemaVersion', 'status', 'value',
    ]);
  });

  it('accepts each route declared status, including middleware and framework statuses', async () => {
    const responses = [
      jsonResponse({ detail: 'run not found' }, 404),
      jsonResponse({ detail: 'unsupported row status filter' }, 400),
      jsonResponse({ detail: 'editor role required' }, 403),
      jsonResponse({ detail: 'run cannot be cancelled' }, 409),
      jsonResponse({ detail: 'invalid stored receipt' }, 500),
      jsonResponse({ detail: 'invalid limit' }, 422),
      jsonResponse({ detail: 'invalid job id' }, 422),
    ];
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return responses.shift()!;
    }));
    const controller = new AbortController();
    const options = { signal: controller.signal };

    await expectApiError(
      getRunProgressContract('project/one', 17, options),
      404,
      'run not found',
    );
    await expectApiError(
      getRunRowsContract(
        'project/one',
        17,
        { offset: 0, limit: 500, status: 'error' },
        options,
      ),
      400,
      'unsupported row status filter',
    );
    await expectApiError(
      cancelRunContract('project/one', 17, options),
      403,
      'editor role required',
    );
    await expectApiError(
      cancelRunContract('project/one', 17, options),
      409,
      'run cannot be cancelled',
    );
    await expectApiError(
      getReceiptContract('project/one', 'receipt/one', options),
      500,
      'invalid stored receipt',
    );
    await expectApiError(
      listActionJobsContract('project/one', { status: null, limit: 50 }, options),
      422,
      'invalid limit',
    );
    await expectApiError(
      getActionJobContract('project/one', 23, options),
      422,
      'invalid job id',
    );

    expect(requests.map((request) => request.input)).toEqual([
      '/api/projects/project%2Fone/actions/runs/17/status',
      '/api/projects/project%2Fone/actions/runs/17/rows?offset=0&limit=500&status=error',
      '/api/projects/project%2Fone/actions/runs/17/cancel',
      '/api/projects/project%2Fone/actions/runs/17/cancel',
      '/api/projects/project%2Fone/actions/v1/receipts/receipt%2Fone',
      '/api/projects/project%2Fone/actions/jobs?limit=50',
      '/api/projects/project%2Fone/actions/jobs/23',
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(controller.signal);
      expect(request.init?.headers).toBeUndefined();
    }
  });
});
