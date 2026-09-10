import { afterEach, describe, expect, it, vi } from 'vitest';
import { columnTablesExportRequest } from '../../src/actions/columnTablesExport';
import { createProjectApi } from '../../src/api/real';
import type { RegisteredActionRequest } from '../../src/api/types';
import type { ExportColumnTablesParams } from '../../src/generated/actionTypes';

afterEach(() => vi.unstubAllGlobals());

describe('column-table export requests', () => {
  it('authors a project-scoped artifact export without a legacy launcher or inferred defaults', () => {
    const request = columnTablesExportRequest({
      sheetId: '7', columnId: '9', groupBy: ' agency ', excludeColumns: [' debug ', '', ' internal '],
    });
    expect(request).toEqual({
      action_id: 'export.column_tables',
      scope: { kind: 'project' },
      params: {
        sheet_id: 7,
        column_id: 9,
        destination: { kind: 'project_file', prefix: 'exports/tables' },
        group_by: 'agency',
        exclude_columns: ['debug', 'internal'],
      },
      output_names: {},
      idempotency_key: expect.stringMatching(/^web-export\.column_tables:/),
    });
  });

  it('omits blank optional controls and gives each new submission a fresh identity', () => {
    const value = { sheetId: '7', columnId: '9', groupBy: ' ', excludeColumns: ['', ' '] };
    const first = columnTablesExportRequest(value);
    const second = columnTablesExportRequest(value);
    expect(first.params).toEqual({
      sheet_id: 7, column_id: 9, destination: { kind: 'project_file', prefix: 'exports/tables' },
    });
    expect(second.idempotency_key).not.toBe(first.idempotency_key);
  });

  it.each(['', 'bad-id', '0', '-1', '1.5'])('rejects invalid source IDs (%s) before transport', (id) => {
    expect(() => columnTablesExportRequest({ sheetId: id, columnId: '9' })).toThrow('valid sheet');
    expect(() => columnTablesExportRequest({ sheetId: '7', columnId: id })).toThrow('valid column');
  });

  it.each(['project_file', 'local_dir'] as const)(
    'posts %s Params unchanged without catalog or sheet-data translation and accepts a runless receipt',
    async (destinationKind) => {
      const request: RegisteredActionRequest = destinationKind === 'project_file'
        ? columnTablesExportRequest({ sheetId: '7', columnId: '9' })
        : {
          action_id: 'export.column_tables', scope: { kind: 'project' }, output_names: {},
          params: {
            sheet_id: 7, column_id: 9,
            destination: { kind: 'local_dir', path: '/tmp/column-tables-output' },
            group_by: 'agency', exclude_columns: ['debug'],
            name_template: '{row:03d}_{table:03d}.csv', first_row_header: false,
          } satisfies ExportColumnTablesParams,
          idempotency_key: 'web-export.column_tables:local-test',
        };
      const calls: Array<{ url: string; body: unknown }> = [];
      vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method !== 'POST') throw new Error(`Unexpected source/catalog read: ${String(input)}`);
        calls.push({ url: String(input), body: JSON.parse(String(init.body)) });
        return new Response(JSON.stringify({
          schema_version: 'frisket.action_result.v1', status: 'completed',
          run_id: null, receipt_id: 'export-receipt', errors: [],
          outputs: [{ kind: 'export', name: 'column_tables', ref: { format: 'zip' } }],
        }), { headers: { 'Content-Type': 'application/json' } });
      }));
      const api = createProjectApi('export-project');
      const result = await api.runAction(request);
      await api.runAction(request);
      expect(calls).toEqual([
        { url: '/api/projects/export-project/actions/v1/run', body: request },
        { url: '/api/projects/export-project/actions/v1/run', body: request },
      ]);
      expect(result).toMatchObject({ runId: null, receiptId: 'export-receipt', status: 'completed' });
    },
  );
});
