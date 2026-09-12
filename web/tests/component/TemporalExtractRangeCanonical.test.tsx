// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';

import type { ActionCatalogPayload, GeneratedActionRequest, SheetMeta } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { completeCatalogPayload } from '../support/actionFormFixtures';
import { createProjectApi } from '../../src/api/real';

let api = createProjectApi('test-project');

const PROJECT = 'temporal-extract-range-canonical';
const SHEET: SheetMeta = {
  id: '7',
  name: 'Interviews',
  rowCount: 12,
  citedColumnIds: [],
  columns: [
    { id: '11', name: 'recording', type: 'video' },
    { id: '12', name: 'alternate_recording', type: 'audio' },
    { id: '13', name: 'reviewed_range', type: 'timeline_range' },
    { id: '14', name: 'reviewed_ranges', type: 'timeline_ranges' },
    { id: '15', name: 'notes', type: 'text' },
  ],
};

const extractEntry = syntheticActionCatalogEntry('temporal.extract_range');

const catalog = completeCatalogPayload([
  extractEntry as unknown as ActionCatalogPayload['actions'][number],
]);

const resolvedTemplates = actionTemplatesFromCatalog(catalog);

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

let mountedStores: WorkspaceStores[] = [];

afterEach(() => {
  cleanup();
  for (const stores of mountedStores) stores.dispose();
  mountedStores = [];
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderPanel({
  actionCatalog = catalog,
  activeRowId,
  selectedRowIds,
  inspectProposal,
  onRun,
}: {
  actionCatalog?: ActionCatalogPayload;
  activeRowId?: string;
  selectedRowIds?: string[];
  inspectProposal?: { seq: number; title: string; spec: Record<string, unknown> };
  onRun: (request: GeneratedActionRequest) => void;
}) {
  const stores = createWorkspaceStores(PROJECT, api);
  mountedStores.push(stores);
  vi.spyOn(stores.projectApi, 'resolveActionParams').mockImplementation(async ({ params, scope }) => {
    const selection = params.selection as { kind?: string; start_ms?: number; end_ms?: number; repeat_for_rows?: boolean };
    const rows = scope.kind === 'sheet_rows' ? scope.row_ids : undefined;
    const valid = selection?.kind === 'column' || (rows?.length
      && (rows.length === 1 || selection?.repeat_for_rows)
      && (selection?.kind === 'typed_value' || (selection?.end_ms ?? 0) > (selection?.start_ms ?? 0)));
    return { diagnostics: valid ? {} : { selection: { ok: false, message: 'Choose a valid range and rows; acknowledge repetition.' } },
      logical_outputs: [{ key: 'clip', column_type: 'video' }] };
  });
  stores.actionCatalog.store.set(() => ({
    status: 'ready',
    error: null,
    catalog: actionCatalog,
    resolvedTemplates,
    version: 1,
  }));
  const renderAtSheet = (sheet: SheetMeta) => (
    <WorkspaceStoresContext.Provider value={stores}>
      <ActionPanel
        sheet={sheet}
        running={false}
        routeActionKind={inspectProposal ? undefined : 'temporal.extract_range'}
        routeActionLaunchId={1}
        activeRowId={activeRowId}
        selectedRowIds={selectedRowIds}
        inspectProposal={inspectProposal}
        onRun={vi.fn()}
        onExecuteRegisteredAction={onRun}
      />
    </WorkspaceStoresContext.Provider>
  );
  const view = render(renderAtSheet(SHEET));
  return {
    rerenderSheet: (sheet: SheetMeta) => view.rerender(renderAtSheet(sheet)),
  };
}

function installRunTransport(posts: Record<string, unknown>[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === `/api/projects/${PROJECT}/actions/v1/run`) {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      posts.push(body);
      return jsonResponse({
        schema_version: 'frisket.action_result.v1',
        action: { kind: body.action_id, action_id: 'extract-range-test' },
        status: 'queued',
        run_id: null,
        job_id: 918,
        receipt_id: 'receipt-extract-range-test',
        outputs: [],
        errors: [],
      });
    }
    throw new Error(`Extract-range canonical launch must not fetch ${path}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function savedSpec(params: Record<string, unknown>, outputName: string, rowIds?: number[]) {
  return {
    action_id: 'temporal.extract_range',
    scope: { kind: 'sheet_rows', sheet_id: 7, ...(rowIds ? { row_ids: rowIds } : {}) },
    params,
    output_names: { clip: outputName },
  };
}

describe('temporal extract-range typed canonical launch', () => {
  it('posts a fresh literal range exactly without catalog or sheet-data refetch', async () => {
    api = createProjectApi(PROJECT);
    vi.stubGlobal('crypto', { randomUUID: () => 'fresh-literal' });
    const posts: Record<string, unknown>[] = [];
    const fetchMock = installRunTransport(posts);
    let pending: Promise<unknown> | undefined;
    renderPanel({
      activeRowId: '101',
      onRun: (request) => { pending = api.runAction(request); },
    });
    const user = userEvent.setup();
    await screen.findByTestId('temporal-extract-params');
    await user.selectOptions(screen.getByTestId('extract-range-source'), 'alternate_recording');
    await user.type(screen.getByTestId('extract-range-start'), '10s');
    await user.type(screen.getByTestId('extract-range-end'), '20s');
    fireEvent.change(await screen.findByTestId('field-output-clip'), {
      target: { value: 'quote_clip' },
    });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    await pending;

    expect(posts).toEqual([{
      action_id: 'temporal.extract_range',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] },
      output_names: { clip: 'quote_clip' },
      params: {
        source: 'alternate_recording',
        selection: { kind: 'draft_range', start_ms: 10_000, end_ms: 20_000, repeat_for_rows: false },
      },
      idempotency_key: 'web-temporal.extract_range:fresh-literal',
    }]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('posts a fresh range-column selection for all rows without row_ids', async () => {
    api = createProjectApi(PROJECT);
    vi.stubGlobal('crypto', { randomUUID: () => 'fresh-column' });
    const posts: Record<string, unknown>[] = [];
    installRunTransport(posts);
    let pending: Promise<unknown> | undefined;
    renderPanel({ onRun: (request) => { pending = api.runAction(request); } });
    const user = userEvent.setup();
    await screen.findByTestId('temporal-extract-params');
    await user.click(screen.getByTestId('extract-range-selection-column'));
    await user.selectOptions(screen.getByTestId('extract-range-source'), 'recording');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    await pending;

    expect(posts[0]?.params).toEqual({
      source: 'recording',
      selection: { kind: 'column', column: 'reviewed_range' },
    });
  });

  it('reconfirms and posts the exact fresh literal multi-row scope', async () => {
    api = createProjectApi(PROJECT);
    vi.stubGlobal('crypto', { randomUUID: () => 'fresh-literal-batch' });
    const posts: Record<string, unknown>[] = [];
    installRunTransport(posts);
    let pending: Promise<unknown> | undefined;
    renderPanel({
      selectedRowIds: ['101', '102'],
      onRun: (request) => { pending = api.runAction(request); },
    });
    const user = userEvent.setup();
    await screen.findByTestId('temporal-extract-params');
    await user.selectOptions(screen.getByTestId('extract-range-source'), 'recording');
    await user.type(screen.getByTestId('extract-range-start'), '10s');
    await user.type(screen.getByTestId('extract-range-end'), '20s');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    await user.click(screen.getByTestId('extract-range-batch-confirm'));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    await pending;

    expect(posts[0]?.params).toEqual({
      source: 'recording',
      selection: { kind: 'draft_range', start_ms: 10_000, end_ms: 20_000, repeat_for_rows: true },
    });
    expect(posts[0]?.scope).toEqual({ kind: 'sheet_rows', sheet_id: 7, row_ids: [101, 102] });
  });

  it('hydrates a saved literal request locally and requires fresh batch acknowledgement', async () => {
    api = createProjectApi(PROJECT);
    vi.stubGlobal('crypto', { randomUUID: () => 'saved-literal' });
    const posts: Record<string, unknown>[] = [];
    installRunTransport(posts);
    let pending: Promise<unknown> | undefined;
    renderPanel({
      inspectProposal: {
        seq: 31,
        title: 'Saved extract',
        spec: savedSpec({
          source: 'alternate_recording',
          selection: { kind: 'draft_range', start_ms: 20_000, end_ms: 40_000, repeat_for_rows: true },
        }, 'saved_clip', [202, 203]),
      },
      activeRowId: '999',
      onRun: (request) => { pending = api.runAction(request); },
    });
    const user = userEvent.setup();
    await screen.findByTestId('temporal-extract-params');
    expect(screen.getByTestId('extract-range-source')).toHaveValue('alternate_recording');
    expect(screen.getByTestId('extract-range-start')).toHaveValue('0:20');
    expect(screen.getByTestId('extract-range-end')).toHaveValue('0:40');
    expect(screen.getByTestId('extract-range-batch-confirm')).not.toBeChecked();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    await user.click(screen.getByTestId('extract-range-batch-confirm'));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(screen.getByTestId('field-output-clip')).toHaveValue('saved_clip');
    await user.click(screen.getByTestId('generated-action-run'));
    await pending;

    expect(posts[0]?.params).toEqual({
      source: 'alternate_recording',
      selection: { kind: 'draft_range', start_ms: 20_000, end_ms: 40_000, repeat_for_rows: true },
    });
    expect(posts[0]?.scope).toEqual({ kind: 'sheet_rows', sheet_id: 7, row_ids: [202, 203] });
    expect(posts[0]?.output_names).toEqual({ clip: 'saved_clip' });
    expect(posts[0]).not.toHaveProperty('confirmation');
  });

  it('hydrates an exact typed saved value without representation coercion', async () => {
    const onRun = vi.fn();
    renderPanel({
      inspectProposal: {
        seq: 35,
        title: 'Typed saved extract',
        spec: savedSpec({
          source: 'alternate_recording',
          selection: { kind: 'draft_range', start_ms: 12_000, end_ms: 18_000 },
        }, 'typed_clip', [205]),
      },
      onRun,
    });
    const user = userEvent.setup();
    await screen.findByTestId('temporal-extract-params');
    expect(screen.getByTestId('extract-range-source')).toHaveValue('alternate_recording');
    expect(screen.getByTestId('extract-range-start')).toHaveValue('0:12');
    expect(screen.getByTestId('extract-range-end')).toHaveValue('0:18');
    expect(await screen.findByTestId('field-output-clip')).toHaveValue('typed_clip');

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));

    expect(onRun).toHaveBeenCalledTimes(1);
    expect(onRun.mock.calls[0]?.[0]).toMatchObject({
      action_id: 'temporal.extract_range',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [205] },
      params: { source: 'alternate_recording',
        selection: { kind: 'draft_range', start_ms: 12_000, end_ms: 18_000 } },
      output_names: { clip: 'typed_clip' },
    });
  });

  it('hydrates a top-level current column selection without borrowing ambient rows', async () => {
    api = createProjectApi(PROJECT);
    vi.stubGlobal('crypto', { randomUUID: () => 'saved-column' });
    const posts: Record<string, unknown>[] = [];
    installRunTransport(posts);
    let pending: Promise<unknown> | undefined;
    renderPanel({
      inspectProposal: {
        seq: 32,
        title: 'Saved column extract',
        spec: savedSpec({
          source: 'recording',
          selection: { kind: 'column', column: 'reviewed_range' },
        }, 'reviewed_clip'),
      },
      activeRowId: '999',
      onRun: (request) => { pending = api.runAction(request); },
    });
    const user = userEvent.setup();
    await screen.findByTestId('temporal-extract-params');
    expect(screen.getByTestId('extract-range-column-select')).toHaveValue('reviewed_range');
    expect(await screen.findByTestId('field-output-clip')).toHaveValue('reviewed_clip');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    await pending;
    expect(posts[0]?.scope).toEqual({ kind: 'sheet_rows', sheet_id: 7 });
    expect(posts[0]?.output_names).toEqual({ clip: 'reviewed_clip' });
    expect(posts[0]?.params).toMatchObject({
      source: 'recording',
      selection: { kind: 'column', column: 'reviewed_range' },
    });
  });

  it('does not retarget a saved extract when the live sheet identity changes', async () => {
    const onRun = vi.fn();
    const view = renderPanel({
      inspectProposal: {
        seq: 38,
        title: 'Saved extract',
        spec: savedSpec({
          source: 'recording',
          selection: { kind: 'column', column: 'reviewed_range' },
        }, 'saved_clip'),
      },
      onRun,
    });
    await screen.findByTestId('temporal-extract-params');

    view.rerenderSheet({ ...SHEET, id: '8', name: 'Other interviews' });

    expect(await screen.findByRole('alert')).toHaveTextContent(/sheet|recreate|unavailable/i);
    expect(screen.queryByTestId('generated-action-run')).not.toBeInTheDocument();
    expect(onRun).not.toHaveBeenCalled();
  });

  it.each([
    ['missing', {
      ...catalog,
      actions: catalog.actions.filter((entry) => entry.kind !== 'temporal.extract_range'),
    }],
    ['duplicate', { ...catalog, actions: [...catalog.actions, extractEntry] }],
  ])('fails closed for a %s exact catalog entry', async (_label, actionCatalog) => {
    const onRun = vi.fn();
    renderPanel({ actionCatalog: actionCatalog as ActionCatalogPayload, onRun });
    expect(await screen.findByRole('alert')).toHaveTextContent(/catalog|unavailable/i);
    expect(screen.queryByTestId('temporal-extract-params')).toBeNull();
    expect(onRun).not.toHaveBeenCalled();
  });
});
