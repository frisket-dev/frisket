import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPdfImportsApi } from '../../src/api/pdfImports';
import { createOnboardingImportsApi } from '../../src/api/onboardingImports';

const telemetry = vi.hoisted(() => vi.fn());
vi.mock('../../src/telemetry/productTelemetry', async (original) => ({
  ...await original<typeof import('../../src/telemetry/productTelemetry')>(), sendProductTelemetry: telemetry,
}));
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), {
  status, headers: { 'Content-Type': 'application/json' },
});
const api = createOnboardingImportsApi((status) => Object.assign(new Error('private provider response'), { status }), 'private-project');
beforeEach(() => telemetry.mockClear());
afterEach(() => vi.unstubAllGlobals());

it('counts a PDF batch commit as queued, never each staging request as an import', async () => {
  const pdf = createPdfImportsApi();
  const fetch = vi.fn(async () => response({ status: 'accepted' }));
  await pdf.create('private-project', 5, 100, 'private file names', { fetch });
  expect(telemetry).not.toHaveBeenCalled();
  await pdf.commit('private-project', 'private-draft', { expected_revision: 1, idempotency_key: 'private-key' }, { fetch });
  expect(telemetry).toHaveBeenCalledTimes(2);
  expect(telemetry).toHaveBeenLastCalledWith({ type: 'Import.submitted', properties: {
    importKind: 'files', format: 'unknown', bytes: 'unknown', rows: 'unknown', columns: 'unknown',
    requestDuration: 'lt_1s', result: 'queued', failureCategory: 'none',
  } }, 'private-project');
  expect(JSON.stringify(telemetry.mock.calls)).not.toContain('private-draft');
});

it('counts URL import partial results without including the URLs', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ sheet_id: 7, rows: 2, downloaded: 2, failed: 1 })));
  await api.importUrls(['https://private.example/']);
  expect(telemetry).toHaveBeenLastCalledWith(expect.objectContaining({ type: 'Import.submitted', properties: expect.objectContaining({
    importKind: 'url', rows: '1_999', result: 'partial', failureCategory: 'unknown',
  }) }), 'private-project');
  expect(JSON.stringify(telemetry.mock.calls)).not.toContain('https://');
});

it('counts mixed bulk results once and omits paths and error text', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({
    created: [{ kind: 'sheet', rows: 4, sheet_id: 9, logical_paths: ['private.csv'] }],
    failed: [{ logical_paths: ['private.pdf'], error: 'sensitive details' }], first_sheet_id: 9,
  })));
  await api.executeBulkImport('private-plan', {});
  expect(telemetry).toHaveBeenCalledTimes(2);
  expect(telemetry).toHaveBeenLastCalledWith(expect.objectContaining({ type: 'Import.submitted', properties: expect.objectContaining({
    importKind: 'files', rows: '1_999', result: 'partial', failureCategory: 'unknown',
  }) }), 'private-project');
  expect(JSON.stringify(telemetry.mock.calls)).not.toMatch(/private\.(csv|pdf)|sensitive details/);
});

it('keeps rejected admission rejected and records only its category', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ detail: 'private rejection' }, 403)));
  await expect(api.importUrls(['https://private.example/'])).rejects.toMatchObject({ status: 403 });
  expect(telemetry).toHaveBeenLastCalledWith(expect.objectContaining({ type: 'Import.submitted', properties: expect.objectContaining({
    result: 'failed', failureCategory: 'permission',
  }) }), 'private-project');
});
