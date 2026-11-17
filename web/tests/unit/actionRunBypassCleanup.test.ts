import { afterEach, describe, expect, it, vi } from 'vitest';

const { httpContractMock } = vi.hoisted(() => ({
  httpContractMock: vi.fn(),
}));

vi.mock('../../src/api/httpContract', () => ({
  httpContract: httpContractMock,
}));

import { createProjectApi } from '../../src/api/real';

const realApi = createProjectApi('project /%?☃');

function completedActionResult() {
  return {
    schema_version: 'frisket.action_result.v1' as const,
    action: { kind: 'test.action', action_id: 'action-1' },
    status: 'completed',
    project_id: 'project/one',
    run_id: null,
    receipt_id: null,
    errors: [],
    outputs: [{ kind: 'sheet', sheet_id: 17 }],
  };
}

afterEach(() => {
  httpContractMock.mockReset();
  vi.unstubAllGlobals();
});

describe('remaining action-run callers', () => {
  it('preserves a completed run-backed materialized sheet in the launch result', async () => {
    httpContractMock.mockImplementation(async (
      _operationId: unknown,
      _options: unknown,
      mapResponse?: (wire: unknown) => unknown,
    ) => {
      const result = {
        ...completedActionResult(),
        run_id: 88,
        receipt_id: 'receipt-88',
      };
      return mapResponse ? mapResponse(result) : result;
    });

    await expect(realApi.runAction({
      action_id: 'derive.test',
      scope: { kind: 'project' },
      params: {},
      output_names: {},
      sheet_name: 'Result',
      idempotency_key: 'materialized-run-88',
    })).resolves.toEqual({
      runId: '88',
      jobId: null,
      receiptId: 'receipt-88',
      status: 'completed',
      outputSheetId: '17',
    });
  });

  it('routes actions through their generated operation', async () => {
    const calls: Array<{ operationId: unknown; options: unknown }> = [];
    httpContractMock.mockImplementation(async (
      operationId: unknown,
      options: unknown,
      mapResponse?: (wire: unknown) => unknown,
    ) => {
      calls.push({ operationId, options });
      const result = completedActionResult();
      return mapResponse ? mapResponse(result) : result;
    });
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('action run must not bypass the generated transport');
    }));
    await expect(realApi.updateEmbeddingIndexPolicy({ indexId: 'idx-1' })).resolves.toBeUndefined();
    await expect(realApi.refreshSheet('7')).resolves.toEqual({
      status: 'completed', sheetId: '7', needsConfirmation: false,
    });

    expect(calls.map((call) => call.operationId)).toEqual([
      'tenant.v1_action_run.post',
      'tenant.v1_action_run.post',
    ]);
    expect(calls.map((call) => call.options)).toEqual([
      expect.objectContaining({ pathParams: { pid: 'project /%?☃' } }),
      expect.objectContaining({ pathParams: { pid: 'project /%?☃' } }),
    ]);
  });
});
