// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { SAVED_ACTION_SPEC_REFUSAL } from '../../src/actions/savedActionSpec';
import * as apiModule from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import type { ActionExecutionRequest, RegisteredActionRequest } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';



const DERIVE_TABLE_ENTRY = syntheticActionCatalogEntry('derive.table_from_list', {
  input_schema: {
    type: 'object',
    additionalProperties: false,
    required: ['source'],
    properties: {
      source: { type: 'object' },
      item_schema: { anyOf: [{ type: 'object' }, { type: 'null' }], default: null },
      columns: { anyOf: [{ type: 'array', items: { type: 'object' } }, { type: 'null' }], default: null },
    },
  },
  required_capabilities: ['project:write'],
  execution_mode: 'whole_project',
  writes_project: true,
  examples: [{
    action_id: 'derive.table_from_list',
    scope: { kind: 'project' },
    sheet_name: 'Entities',
    params: {
      source: {
        kind: 'named_result',
        sheet_id: 1,
        column_id: 4,
        run_id: 2,
        route: 'entities',
        schema: 'entity_list',
      },
      item_schema: {
        type: 'object',
        required: ['name', 'title'],
        properties: {
          name: { type: 'string' },
          title: { type: 'string' },
        },
      },
      columns: [
        { name: 'name', path: '$.name', type: 'text' },
        { name: 'title', path: '$.title', type: 'text' },
      ],
    },
    idempotency_key: 'derive_entities@sha256:...',
  }],
  ui_hints: {
    form: 'generated',
    category: 'convert',
    semantic_controls: {},
    logical_outputs: [],
    dynamic_outputs: true,
    typed_action: { creates_sheet: true },
  },
});


const HIDDEN_ENTRIES = [
  syntheticActionCatalogEntry('derive.collection_expand', {
    input_schema: { type: 'object', additionalProperties: false,
      required: ['source_sheet_id', 'source_column_id', 'source_row_id'], properties: {
        source_sheet_id: { type: 'integer', minimum: 1 },
        source_column_id: { type: 'integer', minimum: 1 },
        source_row_id: { type: 'integer', minimum: 1 },
      } },
    ui_hints: { form: 'generated', semantic_controls: {}, logical_outputs: [
      { key: 'url', column_type: 'link' }, { key: 'title', column_type: 'text' },
      { key: 'video_id', column_type: 'text' }, { key: 'channel_title', column_type: 'text' },
      { key: 'published_at', column_type: 'text' }, { key: 'position', column_type: 'integer' },
    ],
      typed_action: { creates_sheet: true } },
    examples: [{ action_id: 'derive.collection_expand', scope: { kind: 'project' },
      sheet_name: '@investigative videos', params: {
        source_sheet_id: 1, source_column_id: 4, source_row_id: 7,
      }, output_names: {} }],
  }),
];

const servedCatalog = servedActionCatalog();
const CATALOG = { ...servedCatalog, actions: servedCatalog.actions.map((entry) => (
  entry.kind === DERIVE_TABLE_ENTRY.kind ? DERIVE_TABLE_ENTRY
    : HIDDEN_ENTRIES.find((hidden) => hidden.kind === entry.kind) ?? entry
)) };
const TEMPLATES = actionTemplatesFromCatalog(CATALOG);
const SHEET = sheetMeta([
  columnDef({ id: '42', name: 'source', type: 'text' }),
  columnDef({ id: '43', name: 'items', type: 'json' }),
], { id: '7', name: 'Current', rowCount: 3 });

let stores: WorkspaceStores | null = null;

function exampleParams(kind: string): Record<string, unknown> {
  const entry = CATALOG.actions.find((candidate) => candidate.kind === kind);
  const params = entry?.examples[0]?.params;
  if (!params || typeof params !== 'object' || Array.isArray(params)) {
    throw new Error(`Missing example params for ${kind}`);
  }
  return structuredClone(params);
}

function bindPrimarySource(
  params: Record<string, unknown>,
  sheetId: number,
): Record<string, unknown> {
  const bound = structuredClone(params);
  if (Object.prototype.hasOwnProperty.call(bound, 'sheet_id')) bound.sheet_id = sheetId;
  if (Object.prototype.hasOwnProperty.call(bound, 'source_sheet_id')) {
    bound.source_sheet_id = sheetId;
  }
  if (
    bound.source
    && typeof bound.source === 'object'
    && !Array.isArray(bound.source)
    && Object.prototype.hasOwnProperty.call(bound.source, 'sheet_id')
  ) {
    (bound.source as Record<string, unknown>).sheet_id = sheetId;
  }
  return bound;
}

function savedSpec(kind: string, params: Record<string, unknown>, sheetName = 'Entities',
  outputNames: Record<string, string> = {}) {
  if (kind === 'derive.table_from_list' || kind === 'derive.collection_expand') return {
    action_id: kind, scope: { kind: 'project' }, sheet_name: sheetName,
    params, output_names: outputNames,
  };
  return {
    action_id: kind,
    scope: { kind: 'sheet_rows', sheet_id: 7 },
    output_names: outputNames,
    params,
  };
}

function renderInspect(args: {
  kind: string;
  params: Record<string, unknown>;
  selectedRowIds?: string[];
  onRun?: (request: ActionExecutionRequest) => void;
  onExecute?: (request: RegisteredActionRequest) => void;
  sheetName?: string;
  outputNames?: Record<string, string>;
  seq?: number;
}) {
  render(
    <WorkspaceStoresContext.Provider value={stores!}>
      <ActionPanel
        sheet={SHEET}
        running={false}
        selectedRowIds={args.selectedRowIds}
        inspectProposal={{
          seq: args.seq ?? 1,
          title: `Saved ${args.kind}`,
          spec: savedSpec(args.kind, args.params, args.sheetName, args.outputNames),
        }}
        onRun={args.onRun ?? vi.fn()}
        onExecuteRegisteredAction={args.onExecute ?? vi.fn()}
      />
    </WorkspaceStoresContext.Provider>,
  );
}

beforeEach(() => {
  const projectApi = createProjectApi('saved-action-inspect-context');
  stores = createWorkspaceStores('saved-action-inspect-context', projectApi);
  stores.actionCatalog.store.set(() => ({
    status: 'ready',
    error: null,
    catalog: CATALOG,
    resolvedTemplates: TEMPLATES,
    version: 1,
  }));
  vi.spyOn(apiModule, 'getRuntimeConfig').mockResolvedValue({
    cache_mode: 'replay',
    live_calls_possible: true,
    cache_mode_editable: false,
    email_from_address: null,
    email_from_name: null,
    recipe_fence_posture: 'enforced',
  });
  vi.spyOn(projectApi, 'estimateAction').mockResolvedValue({ cost: 0, rows: 3, llm: false });
  vi.spyOn(apiModule, 'listProviders').mockResolvedValue({ schemaVersion: 'frisket.providers.v1', tier: 'local',
    providers: [{ id: 'test', label: 'Test', kind: 'platform_api', configured: true, source: 'env', hint: null,
      models: [{ id: 'test/model', label: 'Test model', price: null }] }] });
  vi.spyOn(projectApi, 'resolveActionParams').mockImplementation(async (request) => ({
    diagnostics: {}, logical_outputs: request.action_id === 'map.extract' ? [{ key: 'items', column_type: 'json' }] : [],
  }));
});

afterEach(() => {
  cleanup();
  stores?.dispose();
  stores = null;
  vi.restoreAllMocks();
});

describe('saved ActionPanel context ownership', () => {
  it.each([
    ['a coercible field type', (params: Record<string, unknown>) => ({ ...params, sheet_id: '7' })],
    ['an extra legacy field', (params: Record<string, unknown>) => ({ ...params, legacy_alias: true })],
  ])('sends original raw params for %s before refusing an invalid saved action', async (_label, mutate) => {
    const raw = mutate(exampleParams('map.clean_column'));
    vi.mocked(stores!.projectApi.resolveActionParams).mockResolvedValue({
      diagnostics: { __all__: { ok: false, message: 'Invalid saved params.' } },
      logical_outputs: [],
    });

    renderInspect({ kind: 'map.clean_column', params: raw });

    expect(await screen.findByRole('alert')).toHaveTextContent(SAVED_ACTION_SPEC_REFUSAL);
    expect(stores!.projectApi.resolveActionParams).toHaveBeenCalledWith({
      action_id: 'map.clean_column',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: raw,
    });
    expect(screen.queryByTestId('generated-action-form')).not.toBeInTheDocument();
  });

  it('keeps resolver transport failures temporary and does not mount the saved form', async () => {
    vi.mocked(stores!.projectApi.resolveActionParams)
      .mockRejectedValue(new Error('resolver temporarily unavailable'));

    renderInspect({ kind: 'map.template', params: exampleParams('map.template') });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Saved action validation is temporarily unavailable. Try again.',
    );
    expect(screen.getByRole('alert')).not.toHaveTextContent('Recreate the action.');
    expect(screen.queryByTestId('generated-action-form')).not.toBeInTheDocument();
  });

  it('keeps an all-rows saved action authoritative when the grid has selected rows', async () => {
    const onExecute = vi.fn();
    renderInspect({
      kind: 'map.template',
      params: exampleParams('map.template'),
      selectedRowIds: ['11', '12'],
      onExecute,
    });

    expect(await screen.findByTestId('generated-action-form')).toBeVisible();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));

    await waitFor(() => expect(onExecute).toHaveBeenCalledTimes(1));
    expect(onExecute.mock.calls[0][0].scope).toEqual({
      kind: 'sheet_rows',
      sheet_id: 7,
    });
    expect(onExecute.mock.calls[0][0].scope).not.toHaveProperty('row_ids');
  });

  it('refuses a mismatched nested source.sheet_id binding before mounting a form', async () => {
    const kind = 'derive.table_from_list';
    renderInspect({
      kind,
      params: bindPrimarySource(exampleParams(kind), 99),
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This saved action reads from sheet 99, but this drawer is open on sheet 7.',
    );
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Open sheet 99 to Inspect it; the proposal can still be run directly.',
    );
  });

  it.each([
    ['derive.collection_expand', 99],
  ])(
    'reports the existing no-launcher disposition for hidden %s Inspect',
    async (kind, sourceSheetId) => {
      renderInspect({
        kind,
        params: bindPrimarySource(exampleParams(kind), sourceSheetId),
      });

      expect(await screen.findByRole('alert')).toHaveTextContent(
        `Action unavailable: ${kind} has no action drawer launcher.`,
      );
    },
  );

});

describe('derive.table_from_list typed Inspect', () => {
  it('refuses a retired list-table envelope without translating its destination', async () => {
    const onExecute = vi.fn();
    render(
      <WorkspaceStoresContext.Provider value={stores!}>
        <ActionPanel sheet={SHEET} running={false} onRun={vi.fn()}
          onExecuteRegisteredAction={onExecute}
          inspectProposal={{ seq: 9, title: 'Retired list table', spec: {
            action_kind: 'derive.table_from_list', authoring_contract_version: 1,
            params: { source: { kind: 'column', sheet_id: 7, column_id: 43 },
              target_sheet_name: 'Old child' },
          } }} />
      </WorkspaceStoresContext.Provider>,
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(SAVED_ACTION_SPEC_REFUSAL);
    expect(screen.queryByTestId('generated-action-form')).not.toBeInTheDocument();
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('edits only the target while preserving an exact column source', async () => {
    const onRun = vi.fn();
    stores!.actionCatalog.store.set((state) => ({
      ...state,
      resolvedTemplates: state.resolvedTemplates.map((template) => (
        template.actionKind === 'derive.table_from_list'
          ? { ...template, defaultPrompt: '' }
          : template
      )),
    }));
    const source = {
      kind: 'column',
      sheet_id: 7,
      column_id: 43,
      include_columns: ['name', 'role'],
    };
    renderInspect({
      kind: 'derive.table_from_list',
      params: { source, item_schema: null, columns: null },
      sheetName: 'Saved child',
      onExecute: onRun,
      seq: 10,
    });

    expect(await screen.findByTestId('generated-action-form')).toBeVisible();
    expect(screen.getByTestId('derive-source-column-select')).toHaveValue('43');
    expect(screen.getByLabelText('Include properties')).toHaveValue('name, role');
    expect(JSON.parse(screen.getByTestId('derive-table-saved-configuration-json').textContent!))
      .toEqual(source);
    expect(screen.queryByTestId('derive-source-mode-ai')).not.toBeInTheDocument();
    expect(screen.getByTestId('field-sheet_name')).toHaveValue('Saved child');
    fireEvent.change(screen.getByTestId('field-sheet_name'), {
      target: { value: '' },
    });
    expect(screen.getByTestId('run-button')).toBeDisabled();
    fireEvent.change(screen.getByTestId('field-sheet_name'), {
      target: { value: 'Edited child' },
    });
    await waitFor(() => expect(screen.getByTestId('run-button')).toBeEnabled());
    expect(screen.queryByTestId('preview-button')).not.toBeInTheDocument();
    expect(screen.getByTestId('generated-action-preview')).toBeEnabled();
    fireEvent.click(screen.getByTestId('run-button'));

    await waitFor(() => expect(onRun).toHaveBeenCalledTimes(1));
    const request = onRun.mock.calls[0][0] as RegisteredActionRequest;
    const expectedParams = { source, item_schema: null, columns: null };
    expect(request).not.toHaveProperty('validatedParams');
    expect(request).not.toHaveProperty('canonicalAction');
    expect(request).toEqual({
      action_id: 'derive.table_from_list',
      scope: { kind: 'project' },
      sheet_name: 'Edited child',
      params: expectedParams,
      output_names: {},
      idempotency_key: expect.stringContaining('derive.table_from_list'),
    });
  });

  it('preserves the complete named-result source, item schema, and column mapping', async () => {
    const onRun = vi.fn();
    const params = bindPrimarySource(exampleParams('derive.table_from_list'), 7);
    renderInspect({
      kind: 'derive.table_from_list',
      params,
      onExecute: onRun,
      outputNames: { name: 'Person' },
      seq: 11,
    });

    expect(await screen.findByTestId('generated-action-form')).toBeVisible();
    expect(screen.getByTestId('derive-table-source-summary')).toHaveTextContent(
      'Named result entities from run 2 on sheet 7',
    );
    expect(JSON.parse(screen.getByTestId('derive-table-saved-configuration-json').textContent!))
      .toEqual(params.source);
    expect(JSON.parse((screen.getByLabelText('Column projection and item schema') as HTMLTextAreaElement).value))
      .toEqual({ columns: params.columns, item_schema: params.item_schema });
    await waitFor(() => expect(screen.getByTestId('run-button')).toBeEnabled());
    fireEvent.click(screen.getByTestId('run-button'));

    await waitFor(() => expect(onRun).toHaveBeenCalledTimes(1));
    const request = onRun.mock.calls[0][0] as RegisteredActionRequest;
    const expectedParams = structuredClone(params);
    expect(request).not.toHaveProperty('validatedParams');
    expect(request).not.toHaveProperty('canonicalAction');
    expect(request).toEqual({
      action_id: 'derive.table_from_list',
      scope: { kind: 'project' },
      sheet_name: 'Entities',
      params: expectedParams,
      output_names: { name: 'Person' },
      idempotency_key: expect.stringContaining('derive.table_from_list'),
    });
  });

  it('authors a fresh AI composite with the ordinary typed Extract form', async () => {
    const onRun = vi.fn();
    render(
      <WorkspaceStoresContext.Provider value={stores!}>
        <ActionPanel
          sheet={SHEET}
          running={false}
          routeActionKind="derive.table_from_list"
          routeActionLaunchId={12}
          onRun={onRun}
          onExecuteRegisteredAction={vi.fn()}
        />
      </WorkspaceStoresContext.Provider>,
    );

    expect(await screen.findByTestId('derive-source-mode-ai')).toBeEnabled();
    fireEvent.click(screen.getByTestId('derive-source-mode-ai'));
    expect(screen.getByLabelText('Field 1 name')).toHaveValue('items');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    await waitFor(() => expect(onRun).toHaveBeenCalledTimes(1));
    expect(onRun.mock.calls[0][0]).toMatchObject({ intent: 'derive_from_extraction' });
    expect(onRun.mock.calls[0][0]).not.toHaveProperty('validatedParams');
    expect(onRun.mock.calls[0][0]).toMatchObject({
      itemField: 'items', extraction: {
        action_id: 'map.extract', scope: { kind: 'sheet_rows', sheet_id: 7 },
        params: { source: ['source'], fields: [{ name: 'items', type: 'list' }] },
      },
    });
  });
});
