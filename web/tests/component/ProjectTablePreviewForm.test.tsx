// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createProjectApi } from '../../src/api/real';
import { ApiError } from '../../src/api/contractErrors';
import type { GeneratedActionDraft } from '../../src/api/types';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';
import { mockLocalProviders, mountTypedForm } from './cutoverUiF1TypedForm';

let dispose: (() => void) | undefined;
beforeEach(() => { mockLocalProviders(); });
afterEach(() => { cleanup(); dispose?.(); dispose = undefined; vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const cases: Array<{ kind: string; params: GeneratedActionDraft['params']; refused: boolean }> = [
  { kind: 'import.files', params: { files: [{ path: '/fixture/report.pdf' }] }, refused: false },
  { kind: 'import.urls', params: { urls: ['https://example.com/report.pdf'] }, refused: true },
];

it.each(cases.flatMap((testCase) => [true, false].map((hasSheet) => ({ ...testCase, hasSheet }))))(
  'launches $kind Preview through real transport (current sheet: $hasSheet); the server owns effect admission',
  async ({ kind, params, refused, hasSheet }) => {
  const requests: Array<{ url: string; body: unknown }> = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === 'POST') requests.push({ url, body: JSON.parse(String(init.body)) });
    if (url.endsWith('/actions/v1/preview')) return new Response(JSON.stringify(refused ? {
      schema_version: 'frisket.action_preview.v1', error: {
        schema_version: 'frisket.action_error.v1', code: 'preview_effect_requires_run',
        message: 'Run the action instead.', action_kind: kind, field: 'kind',
        details: { requires_durable_run: true }, needs_confirmation: false,
      },
    } : { schema_version: 'frisket.action_preview.v1', preview_id: 'table-sample', total: null }), {
      status: refused ? 400 : 202, headers: { 'Content-Type': 'application/json' },
    });
    return new Response('{}', { headers: { 'Content-Type': 'application/json' } });
  }));
  const api = createProjectApi('table-form');
  const { onExecute, dispose: disposeForm } = mountTypedForm({ kind,
    sheet: hasSheet ? sheetMeta([columnDef({ id: '11', name: 'Source', type: 'text' })], { id: '7' }) : null,
    initialDraft: { action_id: kind, scope: { kind: 'project' }, sheet_name: 'Imported', params, output_names: {} },
    resolveParams: async () => ({ diagnostics: {}, logical_outputs: [], creates_sheet: true }),
  });
  dispose = disposeForm;
  let settled: unknown;
  onExecute.mockImplementation(async (request, intent) => {
    if (intent !== 'preview') throw new Error('This test must not run the action');
    try { settled = await api.startPreview(request); } catch (error) { settled = error; }
  });
  await waitFor(() => expect(screen.getByTestId('generated-action-preview')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-preview'));
  await waitFor(() => expect(settled).toBeDefined());
  expect(requests).toEqual([{ url: '/api/projects/table-form/actions/v1/preview', body: {
    action_id: kind, scope: { kind: 'project' }, sheet_name: 'Imported', params,
    output_names: {}, idempotency_key: expect.any(String),
  } }]);
  expect(onExecute).toHaveBeenCalledOnce();
  if (refused) {
    expect(settled).toBeInstanceOf(ApiError);
    expect(settled).toMatchObject({ status: 400, code: 'preview_effect_requires_run', message: 'Run the action instead.' });
  } else {
    expect(settled).toEqual({ previewId: 'table-sample', total: null });
  }
});
