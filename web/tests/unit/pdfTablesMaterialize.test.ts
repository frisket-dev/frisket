import { describe, expect, it } from 'vitest';
import { pdfTablesMaterializeRequest } from '../../src/actions/pdfTablesMaterialize';

describe('PDF table materialization request', () => {
  const intent = { sheetId: '7', columnId: '12', includeColumns: ['vendor', 'amount'],
    targetName: 'Reviewed tables' };
  it('retains exact column identity and metadata-free projection in a fresh typed request', () => {
    const request = pdfTablesMaterializeRequest(intent);
    expect(request).toEqual({
      action_id: 'derive.table_from_list', scope: { kind: 'project' },
      sheet_name: 'Reviewed tables',
      params: { source: { kind: 'column', sheet_id: 7, column_id: 12,
        include_columns: ['vendor', 'amount'] } },
      output_names: {}, idempotency_key: expect.any(String),
    });
    expect(pdfTablesMaterializeRequest(intent).idempotency_key).not.toBe(request.idempotency_key);
  });
  it.each(['0', 'not-a-column', '1.5'])('rejects invalid source identity %s', (columnId) => {
    expect(() => pdfTablesMaterializeRequest({ ...intent, columnId })).toThrow('existing extracted-table column');
  });
});
