import { afterEach, describe, expect, it, vi } from 'vitest';

const { httpContractMock } = vi.hoisted(() => ({
  httpContractMock: vi.fn(),
}));

vi.mock('../../src/api/httpContract', () => ({
  httpContract: httpContractMock,
}));

import { confirmImportRowsDraft, createProjectApi } from '../../src/api/real';

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
  it('routes import confirmation and actions through their generated operations', async () => {
    const calls: Array<{ operationId: unknown; options: unknown }> = [];
    httpContractMock.mockImplementation(async (
      operationId: unknown,
      options: unknown,
      mapResponse?: (wire: unknown) => unknown,
    ) => {
      calls.push({ operationId, options });
      const result = operationId === 'tenant.import_paste_confirm.post'
        ? { sheet_id: 17, rows: 1, columns: ['name'] } : completedActionResult();
      return mapResponse ? mapResponse(result) : result;
    });
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('action run must not bypass the generated transport');
    }));
    await expect(confirmImportRowsDraft('project /%?☃', {
      draft_id: 'paste@draft-1',
      source_kind: 'paste',
      sheet_name: 'Imported rows',
      raw: 'name\nAda\n',
      preview_rows: [{ name: 'Ada' }],
    }, {
      sheetName: 'People',
      columns: [{ key: 'name', name: 'name', type: 'string', include: true }],
    })).resolves.toEqual({ sheet_id: 17, rows: 1, columns: ['name'] });
    await expect(realApi.updateEmbeddingIndexPolicy({ indexId: 'idx-1' })).resolves.toBeUndefined();
    await expect(realApi.refreshSheet('7')).resolves.toEqual({
      status: 'completed', sheetId: '7', needsConfirmation: false,
    });

    expect(calls.map((call) => call.operationId)).toEqual([
      'tenant.import_paste_confirm.post',
      'tenant.v1_action_run.post',
      'tenant.v1_action_run.post',
    ]);
    expect(calls.map((call) => call.options)).toEqual([
      expect.objectContaining({ pathParams: { pid: 'project /%?☃' } }),
      expect.objectContaining({ pathParams: { pid: 'project /%?☃' } }),
      expect.objectContaining({ pathParams: { pid: 'project /%?☃' } }),
    ]);
  });
});
