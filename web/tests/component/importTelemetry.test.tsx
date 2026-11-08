// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useImportUpload } from '../../src/components/importWorkspace/fileIntake';

const api = vi.hoisted(() => ({ csv: vi.fn(), create: vi.fn(), upload: vi.fn(), telemetry: vi.fn() }));
vi.mock('../../src/api/open', () => ({
  importCsv: api.csv, createImportSession: api.create, uploadFilesToImportSession: api.upload,
  importFiles: vi.fn(), importFollowTheMoney: vi.fn(), importXlsx: vi.fn(),
}));
vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({ chromePreferences: { projectId: 'private-project' } }),
}));
vi.mock('../../src/components/importProgressSession', () => ({
  notifyImportSessionChanged: vi.fn(), rememberImportSessionFiles: vi.fn(), setImportSessionBrowserUploading: vi.fn(),
}));
vi.mock('../../src/telemetry/productTelemetry', async (original) => ({
  ...await original<typeof import('../../src/telemetry/productTelemetry')>(), sendProductTelemetry: api.telemetry,
}));
beforeEach(() => { vi.clearAllMocks(); api.create.mockResolvedValue({ import_ref: 'private-session' }); });
afterEach(cleanup);

it.each([
  ['running', 0, 'queued'],
  ['completed', 0, 'success'],
  ['completed', 2, 'partial'],
  ['cancelled', 0, 'cancelled'],
])('records %s admission truthfully without treating it as completed import', async (state, problems, expected) => {
  api.upload.mockResolvedValue({ state, problem_count: problems, committed_rows: 3, sheet_id: 4, import_ref: 'private-session' });
  const { result } = renderHook(() => useImportUpload({ onError: vi.fn() }));
  await act(async () => {
    await result.current.uploadFiles('files', [new File(['x'], 'private.pdf'), new File(['y'], 'private2.pdf')],
      undefined, undefined, undefined, undefined, undefined,
      { bytes: 2, format: 'pdf', directNeedsBulkPlan: false, usesAttachmentSession: true });
  });
  expect(api.telemetry).toHaveBeenLastCalledWith({ type: 'Import.submitted', properties: {
    importKind: 'files', format: 'pdf', bytes: 'lt_1mb', rows: '1_999', columns: 'unknown',
    requestDuration: 'lt_1s', result: expected, failureCategory: expected === 'partial' ? 'unknown' : 'none',
  } }, 'private-project');
  expect(JSON.stringify(api.telemetry.mock.calls)).not.toContain('private.pdf');
});

it('records failed upload admission without sending error details', async () => {
  api.csv.mockRejectedValue(new TypeError('private URL failed'));
  const onError = vi.fn();
  const { result } = renderHook(() => useImportUpload({ onError }));
  await act(async () => {
    await result.current.uploadFiles('csv', [new File(['x'], 'private.csv')],
      undefined, undefined, undefined, undefined, undefined,
      { bytes: 1, format: 'csv', directNeedsBulkPlan: false, usesAttachmentSession: false });
  });
  expect(onError).toHaveBeenCalled();
  expect(api.telemetry).toHaveBeenLastCalledWith(expect.objectContaining({
    type: 'Import.submitted', properties: expect.objectContaining({ result: 'failed', failureCategory: 'network' }),
  }), 'private-project');
  expect(JSON.stringify(api.telemetry.mock.calls)).not.toContain('private URL');
});
