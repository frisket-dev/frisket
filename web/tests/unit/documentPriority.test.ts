import { afterEach, describe, expect, it, vi } from 'vitest';

const { httpContractMock } = vi.hoisted(() => ({
  httpContractMock: vi.fn(),
}));

vi.mock('../../src/api/httpContract', () => ({
  httpContract: httpContractMock,
}));

import { promoteDocumentPriority } from '../../src/api/documentPriority';

afterEach(() => httpContractMock.mockReset());

describe('document priority HTTP boundary', () => {
  it('returns the authoritative promoted run identity', async () => {
    httpContractMock.mockResolvedValue({
      schemaVersion: 'frisket.document_priority.v1',
      promoted: true,
      run_id: 42,
    });

    await expect(promoteDocumentPriority('project-a', {
      sheetId: 7,
      rowId: 11,
      columnId: 13,
    })).resolves.toEqual({ promoted: true, runId: '42' });
    expect(httpContractMock).toHaveBeenCalledWith('tenant.promote_document_priority.post', {
      pathParams: { pid: 'project-a' },
      query: {},
      body: { sheet_id: 7, row_id: 11, column_id: 13 },
    });
  });

  it('keeps a no-op response without a run identity', async () => {
    httpContractMock.mockResolvedValue({
      schemaVersion: 'frisket.document_priority.v1',
      promoted: false,
      run_id: null,
    });

    await expect(promoteDocumentPriority('project-a', {
      sheetId: 7,
      rowId: 11,
      columnId: 13,
    })).resolves.toEqual({ promoted: false, runId: null });
  });
});
