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
  it('returns the import whose page reading was promoted', async () => {
    httpContractMock.mockResolvedValue({
      schemaVersion: 'frisket.document_priority.v1',
      promoted: true,
      import_ref: 'import-7',
    });

    await expect(promoteDocumentPriority('project-a', {
      sheetId: 7,
      rowId: 11,
      columnId: 13,
    })).resolves.toEqual({ promoted: true, importRef: 'import-7' });
    expect(httpContractMock).toHaveBeenCalledWith('tenant.promote_document_priority.post', {
      pathParams: { pid: 'project-a' },
      body: { sheet_id: 7, row_id: 11, column_id: 13 },
    });
  });

  it('keeps a no-op response without an import identity', async () => {
    httpContractMock.mockResolvedValue({
      schemaVersion: 'frisket.document_priority.v1',
      promoted: false,
      import_ref: null,
    });

    await expect(promoteDocumentPriority('project-a', {
      sheetId: 7,
      rowId: 11,
      columnId: 13,
    })).resolves.toEqual({ promoted: false, importRef: null });
  });
});
