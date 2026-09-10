// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';

import type { ActionCatalogPayload } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { columnDef } from '../support/domainFixtures';
import { completeCatalogPayload, sheetMeta } from '../support/actionFormFixtures';
import { mockActionApiDefaults } from '../support/renderActionForm';
import { createProjectApi } from '../../src/api/real';

let api = createProjectApi('test-project');

interface Deferred<T> {
  promise: Promise<T>;
  resolve(value: T): void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const SHEET_DATA_WIRE = {
  total: 1,
  columns: [
    {
      id: 11,
      name: 'source',
      type: 'text',
      ai_generated: false,
      format: null,
      current_run_id: null,
      transcript_status: null,
      media_download_candidate: null,
    },
  ],
  rows: [{ id: 1, cells: {}, meta: {}, parent_row_id: null, child_count: 0 }],
};

const RUN_COMPLETED_WIRE = {
  schema_version: 'frisket.action_result.v1',
  status: 'completed',
  run_id: null,
  job_id: null,
  receipt_id: null,
  outputs: [],
  errors: [],
};

let stores: WorkspaceStores | null = null;

afterEach(() => {
  cleanup();
  stores?.dispose();
  stores = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('action presentation wire', () => {
  it('posts clean-column as the generated registered request', async () => {
    api = createProjectApi('p1');
    let runInit: RequestInit | undefined;
    vi.stubGlobal('crypto', { randomUUID: () => 'action-03a-wire-pin' });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? 'GET').toUpperCase();
        if (method === 'GET' && url.startsWith('/api/projects/p1/sheets/1/data')) {
          return jsonResponse(SHEET_DATA_WIRE);
        }
        if (method === 'POST' && url === '/api/projects/p1/actions/v1/validate-params') {
          return jsonResponse({
            schema_version: 'frisket.action_param_validation_result.v1',
            action: { kind: 'map.clean_column' },
            project_id: 'p1',
            diagnostics: {},
            logical_outputs: [],
          });
        }
        if (method === 'POST' && url === '/api/projects/p1/actions/v1/run') {
          runInit = init;
          return jsonResponse(RUN_COMPLETED_WIRE);
        }
        throw new Error(`Unexpected fetch: ${method} ${url}`);
      }),
    );

    const catalog = completeCatalogPayload();
    const resolved = actionTemplatesFromCatalog(catalog);
    stores = createWorkspaceStores('p1', api);
    stores.actionCatalog.store.set(() => ({
      status: 'ready',
      error: null,
      catalog,
      resolvedTemplates: resolved,
      version: 1,
    }));

    let runPromise: Promise<unknown> | null = null;
    render(
      <WorkspaceStoresContext.Provider value={stores}>
        <ActionPanel
          sheet={sheetMeta(
            [columnDef({ id: '11', name: 'source', type: 'text' })],
            { id: '1' },
          )}
          running={false}
          routeActionKind="map.clean_column"
          routeActionInitial={{ sourceColumn: 'source' }}
          routeActionLaunchId={1}
          onRun={vi.fn()}
          onExecuteRegisteredAction={(request, intent) => {
            if (intent !== 'run') return;
            runPromise = api.runAction(request);
          }}
        />
      </WorkspaceStoresContext.Provider>,
    );

    await waitFor(() => expect(screen.getByTestId('field-case')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('field-case'), { target: { value: 'keep' } });
    fireEvent.change(screen.getByTestId('field-null_tokens'), {
      target: { value: 'missing,unknown' },
    });
    expect(screen.getByTestId('field-blank_null_tokens')).toBeChecked();
    fireEvent.click(screen.getByTestId('field-blank_null_tokens'));
    expect(screen.getByTestId('field-blank_null_tokens')).not.toBeChecked();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));

    await waitFor(() => expect(runInit).toBeDefined());
    await runPromise;
    const body = JSON.parse(String(runInit?.body)) as {
      params: Record<string, unknown>;
    };
    expect(typeof body.params.blank_null_tokens).toBe('boolean');
    expect(body).toEqual({
      action_id: 'map.clean_column',
      scope: { kind: 'sheet_rows', sheet_id: 1 },
      params: {
        source: 'source',
        case: 'keep',
        null_tokens: 'missing,unknown',
        blank_null_tokens: false,
        lowercase_emails: true,
        normalize_us_phone: true,
        expand_abbreviations: true,
        reorder_person_name: true,
        canonicalize_duplicates: true,
        strip_edge_punct: false,
        normalize_unicode_punct: false,
        remove_thousands_separators: false,
        remove_all_commas: false,
        make_numeric: false,
      },
      output_names: { cleaned: 'source_clean' },
      idempotency_key: 'web-map.clean_column:action-03a-wire-pin',
    });
  });

});

describe('action presentation catalog refresh continuity', () => {
  it('keeps unit-aware capture limits canonical and mounted while the real catalog resource refreshes', async () => {
    api = createProjectApi('p1');
    mockActionApiDefaults();
    const catalog = completeCatalogPayload();
    const catalogApi = vi.spyOn(api, 'listActionCatalog').mockResolvedValueOnce(catalog);
    vi.spyOn(api, 'resolveActionParams').mockImplementation(async ({ params }) => ({
      diagnostics: Object.fromEntries(['max_bytes', 'timeout_ms'].flatMap((key) =>
        params[key] != null && (!Number.isFinite(params[key]) || Number(params[key]) <= 0)
          ? [[key, { ok: false, message: 'Enter a positive number.' }]] : [])),
      logical_outputs: [{ key: 'page', column_type: 'file' }], creates_sheet: false,
    }));
    vi.spyOn(api, 'estimateAction').mockResolvedValue({ rows: 1, cost: 0 } as never);
    let runInit: RequestInit | undefined;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (method === 'GET' && url.startsWith('/api/projects/p1/sheets/1/data')) {
        return jsonResponse(SHEET_DATA_WIRE);
      }
      if (method === 'POST' && url === '/api/projects/p1/actions/v1/run') {
        runInit = init;
        return jsonResponse(RUN_COMPLETED_WIRE);
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    stores = createWorkspaceStores('p1', api);
    await stores.actionCatalog.start();
    expect(stores.actionCatalog.store.get().status).toBe('ready');
    let runPromise: Promise<unknown> | null = null;
    const submittedParams: Readonly<Record<string, unknown>>[] = [];
    render(
      <WorkspaceStoresContext.Provider value={stores}>
        <ActionPanel
          sheet={sheetMeta(
            [columnDef({ id: '11', name: 'source', type: 'link' })],
            { id: '1' },
          )}
          running={false}
          routeActionKind="web.capture_page"
          routeActionInitial={{ sourceColumn: 'source' }}
          routeActionLaunchId={92}
          onRun={vi.fn()}
          onExecuteRegisteredAction={(request) => {
            submittedParams.push(request.params);
            runPromise = api.runAction(request);
          }}
        />
      </WorkspaceStoresContext.Provider>,
    );

    const advanced = await screen.findByTestId('page-capture-advanced');
    fireEvent.click(advanced.querySelector('summary')!);
    const byteField = screen.getByTestId('field-max_bytes');
    const byteUnit = screen.getByTestId('field-max_bytes-unit');
    const timeoutField = screen.getByTestId('field-timeout_ms');
    const timeoutUnit = screen.getByTestId('field-timeout_ms-unit');
    expect(byteField).toHaveValue('5');
    expect(byteUnit).toHaveValue('MB');
    expect(timeoutField).toHaveValue('30');
    expect(timeoutUnit).toHaveValue('s');
    fireEvent.change(byteUnit, { target: { value: 'KB' } });
    fireEvent.change(timeoutUnit, { target: { value: 'ms' } });
    expect(byteField).toHaveValue('5000');
    expect(timeoutField).toHaveValue('30000');
    fireEvent.change(byteField, { target: { value: '7500' } });
    fireEvent.change(timeoutField, { target: { value: '45500' } });

    const pendingCatalog = deferred<ActionCatalogPayload>();
    catalogApi.mockReturnValueOnce(pendingCatalog.promise);
    let refresh!: Promise<void>;
    act(() => {
      refresh = stores!.actionCatalog.refresh();
    });

    expect(stores.actionCatalog.store.get().status).toBe('loading');
    expect(screen.getByTestId('field-max_bytes')).toBe(byteField);
    expect(screen.getByTestId('field-timeout_ms')).toBe(timeoutField);
    expect(screen.getByTestId('field-max_bytes-unit')).toBe(byteUnit);
    expect(screen.getByTestId('field-timeout_ms-unit')).toBe(timeoutUnit);
    expect(byteField).toHaveValue('7500');
    expect(byteUnit).toHaveValue('KB');
    expect(timeoutField).toHaveValue('45500');
    expect(timeoutUnit).toHaveValue('ms');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(fetchMock).not.toHaveBeenCalled();

    await act(async () => {
      pendingCatalog.resolve(catalog);
      await refresh;
    });

    expect(stores.actionCatalog.store.get().status).toBe('ready');
    expect(screen.getByTestId('field-max_bytes')).toBe(byteField);
    expect(screen.getByTestId('field-timeout_ms')).toBe(timeoutField);
    expect(screen.getByTestId('field-max_bytes-unit')).toBe(byteUnit);
    expect(screen.getByTestId('field-timeout_ms-unit')).toBe(timeoutUnit);
    expect(byteField).toHaveValue('7500');
    expect(byteUnit).toHaveValue('KB');
    expect(timeoutField).toHaveValue('45500');
    expect(timeoutUnit).toHaveValue('ms');
    expect(catalogApi).toHaveBeenCalledTimes(2);

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    await waitFor(() => expect(runInit).toBeDefined());
    await runPromise;
    const body = JSON.parse(String(runInit?.body)) as { params: Record<string, unknown> };
    expect(body.params.max_bytes).toBe(7_500_000);
    expect(body.params.timeout_ms).toBe(45_500);
    expect(typeof body.params.max_bytes).toBe('number');
    expect(typeof body.params.timeout_ms).toBe('number');

    fireEvent.change(byteField, { target: { value: '' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeDisabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(submittedParams).toHaveLength(1);

    fireEvent.change(byteField, { target: { value: '6400' } });
    fireEvent.change(timeoutField, { target: { value: 'not-a-duration' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeDisabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(submittedParams).toHaveLength(1);
  });
});
