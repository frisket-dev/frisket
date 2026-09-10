// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';

import type { ActionCatalogPayload, GeneratedActionDraft, GeneratedActionRequest, SheetMeta } from '../../src/api/types';
import { decodeSavedActionSpec, encodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { completeCatalogPayload } from '../support/actionFormFixtures';
import { createProjectApi } from '../../src/api/real';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';

let api = createProjectApi('test-project');

const PROJECT = 'temporal-segments-canonical';
const SHEET: SheetMeta = {
  id: '7',
  name: 'Interviews',
  rowCount: 12,
  citedColumnIds: [],
  columns: [
    { id: '11', name: 'recording', type: 'video' },
    { id: '12', name: 'alternate_recording', type: 'video' },
    { id: '13', name: 'reviewed_cuts', type: 'timeline_points' },
    { id: '14', name: 'reviewed_ranges', type: 'timeline_ranges' },
    { id: '15', name: 'timestamped_transcript', type: 'timestamped_transcript' },
  ],
};

const mediaEntry = syntheticActionCatalogEntry('derive.temporal_segments');

const transcriptEntry = syntheticActionCatalogEntry('derive.transcript_segments', {
  title: 'Split transcript',
  input_schema: { type: 'object', additionalProperties: false, required: ['source', 'selection'],
    properties: { source: { type: 'string' }, selection: { type: 'object' } } },
  required_capabilities: ['project:read', 'project:write'],
  async_mode: 'queued',
  row_scope_policy: { kind: 'sheet_rows', selectors: ['all_rows', 'exact_membership'] },
  ui_hints: { form: 'generated', category: 'convert',
    semantic_controls: { source: 'column', selection: 'transcript_selection' },
    typed_action: { creates_sheet: true }, dynamic_outputs: true,
    logical_outputs: [
      { key: 'transcript', column_type: 'timestamped_transcript' },
      { key: 'source_range', column_type: 'timeline_range' },
    ],
    source_requirements: [{ id: 'source', mode: 'column', param: 'source',
      label: 'Timestamped transcript', min: 1, max: 1,
      accepted_column_types: ['timestamped_transcript'] }],
  },
});

const temporalEntries = [
    mediaEntry,
    transcriptEntry,
] as unknown as ActionCatalogPayload['actions'];
const catalog = completeCatalogPayload(temporalEntries);

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
  routeActionKind,
  activeRowId,
  inspectProposal,
  onRun,
  onExecute,
  selectedRowIds,
}: {
  actionCatalog?: ActionCatalogPayload;
  routeActionKind?: 'derive.temporal_segments' | 'derive.transcript_segments';
  activeRowId?: string;
  inspectProposal?: { seq: number; title: string; spec: Record<string, unknown> };
  onRun: (request: GeneratedActionRequest) => void;
  onExecute?: (request: GeneratedActionRequest) => void;
  selectedRowIds?: string[];
}) {
  const stores = createWorkspaceStores(PROJECT, api);
  mountedStores.push(stores);
  vi.spyOn(stores.projectApi, 'resolveActionParams').mockImplementation(async ({ params, scope }) => {
    const selection = params.selection as { kind?: string; repeat_for_rows?: boolean; items?: unknown[] } | undefined;
    const literal = selection?.kind !== 'column';
    const rowIds = scope.kind === 'sheet_rows' ? scope.row_ids : undefined;
    const valid = selection && (!literal || (rowIds?.length
      && (rowIds.length === 1 || selection.repeat_for_rows)
      && (!selection.kind?.startsWith('draft_') || selection.items?.length)));
    return { diagnostics: valid ? {} : { selection: { ok: false, message: 'Choose rows and a valid selection; acknowledge repetition for multiple rows.' } },
      logical_outputs: params.source === 'timestamped_transcript'
        ? [...transcriptEntry.ui_hints.logical_outputs!, { key: 'speaker', column_type: 'text' }]
        : mediaEntry.ui_hints.logical_outputs! };
  });
  stores.actionCatalog.store.set(() => ({
    status: 'ready',
    error: null,
    catalog: actionCatalog,
    resolvedTemplates,
    version: 1,
  }));
  return render(
    <WorkspaceStoresContext.Provider value={stores}>
      <ActionPanel
        sheet={SHEET}
        running={false}
        routeActionKind={routeActionKind}
        routeActionLaunchId={1}
        activeRowId={activeRowId}
        selectedRowIds={selectedRowIds}
        inspectProposal={inspectProposal}
        onRun={vi.fn()}
        onExecuteRegisteredAction={onExecute ?? onRun}
      />
    </WorkspaceStoresContext.Provider>,
  );
}

function installRunTransport(posts: Record<string, unknown>[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === `/api/projects/${PROJECT}/actions/v1/run`) {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      posts.push(body);
      return jsonResponse({
        schema_version: 'frisket.action_result.v1',
        action: { kind: body.action_id, action_id: 'temporal-segments-test' },
        status: 'queued',
        run_id: null,
        job_id: 917,
        receipt_id: 'receipt-temporal-segments-test',
        outputs: [],
        errors: [],
      });
    }
    throw new Error(`Temporal canonical launch must not fetch ${path}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}


describe('temporal child-sheet typed canonical launch', () => {
  it('posts fresh media points exactly without catalog or sheet-data refetch', async () => {
    api = createProjectApi(PROJECT);
    vi.stubGlobal('crypto', { randomUUID: () => 'fresh-media' });
    const posts: Record<string, unknown>[] = [];
    const fetchMock = installRunTransport(posts);
    let pending: Promise<unknown> | undefined;
    renderPanel({
      routeActionKind: 'derive.temporal_segments',
      activeRowId: '101',
      onRun: (request) => { pending = api.runAction(request); },
    });
    const user = userEvent.setup();
    await screen.findByTestId('media-segments-params');
    await user.selectOptions(screen.getByTestId('split-source'), 'alternate_recording');
    fireEvent.change(screen.getByTestId('split-points-textarea'), {
      target: { value: '60s,Opening\n60s,Repeat observation' },
    });
    fireEvent.change(screen.getByTestId('field-sheet_name'), {
      target: { value: 'Interview sections' },
    });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    await pending;

    expect(posts).toEqual([{
      action_id: 'derive.temporal_segments',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] },
      sheet_name: 'Interview sections', output_names: {},
      params: {
        source: 'alternate_recording',
        selection: {
          kind: 'draft_points', repeat_for_rows: false,
          items: [
            { at_ms: 60_000, label: 'Opening' },
            { at_ms: 60_000, label: 'Repeat observation' },
          ],
        },
      },
      idempotency_key: 'web-derive.temporal_segments:fresh-media',
    }]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(posts[0])).not.toContain('consented_promise_set_hash');
  });

  it('posts fresh transcript column selection for all rows without row_ids', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'fresh-transcript' });
    const onExecute = vi.fn();
    renderPanel({
      routeActionKind: 'derive.transcript_segments',
      onRun: vi.fn(), onExecute,
    });
    const user = userEvent.setup();
    await screen.findByTestId('transcript-segments-params');
    await user.click(screen.getByTestId('split-selection-column'));
    fireEvent.change(screen.getByTestId('field-sheet_name'), {
      target: { value: 'Reviewed transcript' },
    });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));

    expect(onExecute.mock.calls[0][0]).toEqual({
      action_id: 'derive.transcript_segments',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      sheet_name: 'Reviewed transcript', output_names: {},
      params: {
        source: 'timestamped_transcript',
        selection: { kind: 'column', column: 'reviewed_cuts' },
      },
      idempotency_key: expect.any(String),
    });
  });

  it('hydrates a saved media request locally and requires fresh batch acknowledgement', async () => {
    api = createProjectApi(PROJECT);
    vi.stubGlobal('crypto', { randomUUID: () => 'saved-media' });
    const posts: Record<string, unknown>[] = [];
    installRunTransport(posts);
    let pending: Promise<unknown> | undefined;
    renderPanel({
      inspectProposal: {
        seq: 31,
        title: 'Saved media split',
        spec: {
          action_id: 'derive.temporal_segments',
          scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [202, 203] },
          sheet_name: 'Saved media sections', output_names: { clip: 'clip' },
          params: { source: 'alternate_recording', selection: {
            kind: 'draft_points', repeat_for_rows: true,
            items: [{ at_ms: 20_000, label: 'Saved cut' }],
          } },
        },
      },
      onRun: (request) => { pending = api.runAction(request); },
    });
    const user = userEvent.setup();
    await screen.findByTestId('media-segments-params');
    expect(screen.getByTestId('split-source')).toHaveValue('alternate_recording');
    expect(screen.getByTestId('split-points-textarea')).toHaveValue('0:20,Saved cut');
    expect(screen.getByTestId('field-sheet_name')).toHaveValue('Saved media sections');
    expect(screen.getByTestId('split-batch-confirm')).not.toBeChecked();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();

    await user.click(screen.getByTestId('split-batch-confirm'));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    await pending;
    expect(posts[0]?.params).toEqual({
      source: 'alternate_recording',
      selection: {
        kind: 'draft_points', repeat_for_rows: true,
        items: [{ at_ms: 20_000, label: 'Saved cut' }],
      },
    });
    expect(posts[0]?.scope).toEqual({ kind: 'sheet_rows', sheet_id: 7, row_ids: [202, 203] });
    expect(posts[0]?.sheet_name).toBe('Saved media sections');
    expect(posts[0]?.output_names).toEqual({ clip: 'clip' });
    expect(posts[0]).not.toHaveProperty('confirmation');
  });

  it('hydrates a top-level transcript column wire without borrowing ambient rows', async () => {
    const onExecute = vi.fn();
    const draft: GeneratedActionDraft = { action_id: 'derive.transcript_segments',
      scope: { kind: 'sheet_rows', sheet_id: 7 }, sheet_name: 'Saved transcript sections',
      output_names: { transcript: 'Excerpt', source_range: 'Original range', speaker: 'Speaker name' },
      params: { source: 'timestamped_transcript', selection: { kind: 'column', column: 'reviewed_ranges' } } };
    expect(encodeSavedActionSpec(decodeSavedActionSpec(catalog, draft))).toEqual(draft);
    renderPanel({
      activeRowId: '999',
      inspectProposal: {
        seq: 32,
        title: 'Saved transcript split',
        spec: draft,
      },
      onRun: vi.fn(), onExecute,
    });
    const user = userEvent.setup();
    await screen.findByTestId('transcript-segments-params');
    expect(screen.getByTestId('split-selection-column-select')).toHaveValue('reviewed_ranges');
    expect(screen.getByTestId('field-sheet_name')).toHaveValue('Saved transcript sections');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toEqual({ ...draft, idempotency_key: expect.any(String) });
  });

  it('uses the active row for fresh literals and cannot submit stale valid text after an invalid edit', async () => {
    const onExecute = vi.fn();
    renderPanel({ routeActionKind: 'derive.transcript_segments', activeRowId: '101', onRun: vi.fn(), onExecute });
    await screen.findByTestId('transcript-segments-params');
    fireEvent.change(screen.getByTestId('split-points-textarea'), { target: { value: '20s,Opening' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.change(screen.getByTestId('split-points-textarea'), { target: { value: '20s\ninvalid' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeDisabled());
    expect(onExecute).not.toHaveBeenCalled();
    fireEvent.change(screen.getByTestId('split-points-textarea'), { target: { value: '20s,Opening' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toMatchObject({ scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] },
      params: { source: 'timestamped_transcript', selection: { kind: 'draft_points',
        items: [{ at_ms: 20_000, label: 'Opening' }], repeat_for_rows: false } } });
  });

  it('requires fresh acknowledgement after reopening and preserves it when only output names change', async () => {
    const onExecute = vi.fn();
    const draft: GeneratedActionDraft = { action_id: 'derive.transcript_segments',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [202, 203] },
      sheet_name: 'Saved excerpts', output_names: { transcript: 'Excerpt', speaker: 'Speaker name' },
      params: { source: 'timestamped_transcript', selection: { kind: 'draft_ranges',
        items: [{ start_ms: 10_000, end_ms: 20_000, label: 'Opening' }], repeat_for_rows: true } } };
    expect(encodeSavedActionSpec(decodeSavedActionSpec(catalog, draft))).toEqual(draft);
    renderPanel({ activeRowId: '999', selectedRowIds: ['888'], onRun: vi.fn(), onExecute,
      inspectProposal: { seq: 33, title: 'Saved transcript ranges', spec: draft } });
    await screen.findByTestId('transcript-segments-params');
    expect(screen.getByTestId('split-ranges-textarea')).toHaveValue('0:10,0:20,Opening');
    expect(screen.getByTestId('split-batch-confirm')).not.toBeChecked();
    expect(screen.getByTestId('split-batch-confirm').parentElement).toHaveTextContent('2 rows');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeDisabled());
    fireEvent.click(screen.getByTestId('split-batch-confirm'));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.change(screen.getByTestId('field-sheet_name'), { target: { value: 'Renamed excerpts' } });
    expect(screen.getByTestId('split-batch-confirm')).toBeChecked();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toEqual({ ...draft, sheet_name: 'Renamed excerpts', idempotency_key: expect.any(String) });
    expect(onExecute.mock.calls[0][0]).not.toHaveProperty('confirmation');
    expect(onExecute.mock.calls[0][0].params).not.toHaveProperty('confirmed');
  });

  it.each([
    ['missing', {
      ...catalog,
      actions: catalog.actions.filter((entry) => entry.kind !== 'derive.temporal_segments'),
    }],
    ['duplicate', { ...catalog, actions: [...catalog.actions, temporalEntries[0]!] }],
  ])('fails closed for a %s media catalog entry', async (_label, actionCatalog) => {
    const onRun = vi.fn();
    renderPanel({
      actionCatalog: actionCatalog as ActionCatalogPayload,
      routeActionKind: 'derive.temporal_segments',
      activeRowId: '101',
      onRun,
    });
    expect(await screen.findByRole('alert')).toHaveTextContent(/catalog|unavailable/i);
    expect(screen.queryByTestId('media-segments-params')).toBeNull();
    expect(onRun).not.toHaveBeenCalled();
  });

});
