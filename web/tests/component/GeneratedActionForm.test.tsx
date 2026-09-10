// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ActionParamResolution, ActionTemplate, GeneratedActionCatalogEntry } from '../../src/api/types';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { ActionPanel } from '../../src/components/ActionPanel';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { SAVED_ACTION_SPEC_REFUSAL } from '../../src/actions/savedActionSpec';
import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { sheetMeta } from '../support/actionFormFixtures';
import { completeCatalogPayload } from '../support/actionFormFixtures';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { aiMeta, columnDef } from '../support/domainFixtures';

const SHEET = sheetMeta([
  columnDef({ id: '11', name: 'raw', type: 'text' }),
  columnDef({ id: '12', name: 'name', type: 'text' }),
], { id: '7', name: 'People', rowCount: 2 });

let stores: WorkspaceStores | null = null;
let requestSequence = 0;
const resolveStaticParams = async () => ({ diagnostics: {}, logical_outputs: [] });

function generatedEntry(
  kind:
    | 'map.template'
    | 'map.clean_column'
    | 'map.clean_dates'
    | 'map.to_geo_point'
    | 'map.regex_extract'
    | 'map.columns_from_json',
): GeneratedActionCatalogEntry {
  if (kind === 'map.template') {
    return syntheticActionCatalogEntry(kind, {
      title: 'Template',
      input_schema: {
        type: 'object',
        required: ['template'],
        properties: { template: { type: 'object', title: 'Template', additionalProperties: false,
          required: ['text'], properties: { text: { type: 'string' } } } },
      },
      ui_hints: {
        form: 'generated',
        category: 'text',
        semantic_controls: { template: 'template' },
        logical_outputs: [{ key: 'rendered', column_type: 'text' }],
      },
    }) as GeneratedActionCatalogEntry;
  }
  if (kind === 'map.clean_dates') return syntheticActionCatalogEntry(kind, {
    title: 'Parse strict dates',
    input_schema: {
      type: 'object',
      required: ['source'],
      properties: {
        source: { type: 'string', title: 'Source' },
        format: { default: null, title: 'Format' },
      },
    },
    ui_hints: {
      form: 'generated',
      category: 'cleanup',
      semantic_controls: { source: 'column' },
      logical_outputs: [{ key: 'cleaned', column_type: 'date' }],
    },
  }) as GeneratedActionCatalogEntry;
  if (kind === 'map.clean_column') return syntheticActionCatalogEntry(kind, {
    title: 'Clean one text column',
    input_schema: {
      type: 'object',
      required: ['source'],
      properties: {
        source: { type: 'string', title: 'Source' },
        case: {
          type: 'string',
          enum: ['keep', 'smart_title', 'title', 'upper', 'lower'],
          default: 'smart_title',
        },
        null_tokens: { type: 'string', default: 'n/a,na,null,none,unknown' },
        blank_null_tokens: { type: 'boolean', default: true },
        lowercase_emails: { type: 'boolean', default: true },
        normalize_us_phone: { type: 'boolean', default: true },
        expand_abbreviations: { type: 'boolean', default: true },
        reorder_person_name: { type: 'boolean', default: true },
        canonicalize_duplicates: { type: 'boolean', default: true },
        strip_edge_punct: { type: 'boolean', default: false },
        normalize_unicode_punct: { type: 'boolean', default: false },
        remove_thousands_separators: { type: 'boolean', default: false },
        remove_all_commas: { type: 'boolean', default: false },
        make_numeric: { type: 'boolean', default: false },
      },
    },
    ui_hints: {
      form: 'generated',
      category: 'cleanup',
      semantic_controls: { source: 'column' },
      source_requirements: [{
        id: 'source', mode: 'column', param: 'source', label: 'Column to clean',
        min: 1, accepted_column_types: ['text', 'category', 'link'],
      }],
      logical_outputs: [{ key: 'cleaned', column_type: 'text' }],
    },
  }) as GeneratedActionCatalogEntry;
  if (kind === 'map.regex_extract') return syntheticActionCatalogEntry(kind, {
    input_schema: {
      type: 'object',
      required: ['input_columns', 'pattern'],
      properties: {
        input_columns: { type: 'array', items: { type: 'string' }, title: 'Input columns' },
        pattern: { type: 'string', title: 'Pattern' },
        all_matches: { type: 'boolean', default: false, title: 'All matches' },
        group: { default: null, title: 'Group' },
        timeout_seconds: { type: 'number', default: 0.05, title: 'Timeout' },
      },
    },
    ui_hints: {
      form: 'generated',
      category: 'extract',
      semantic_controls: { input_columns: 'columns' },
      source_requirements: [{
        id: 'input_columns', mode: 'columns', param: 'input_columns', label: 'Input columns',
        min: 1, accepted_column_types: ['text'],
      }],
      logical_outputs: [],
      dynamic_outputs: true,
    },
  }) as GeneratedActionCatalogEntry;
  if (kind === 'map.columns_from_json') {
    return syntheticActionCatalogEntry(kind) as GeneratedActionCatalogEntry;
  }
  return syntheticActionCatalogEntry(kind, {
    title: 'Convert latitude/longitude to geo_point',
    input_schema: {
      type: 'object',
      required: ['latitude_column', 'longitude_column'],
      properties: {
        latitude_column: { type: 'string', title: 'Latitude column' },
        longitude_column: { type: 'string', title: 'Longitude column' },
      },
    },
    ui_hints: {
      form: 'generated',
      category: 'convert',
      semantic_controls: {
        latitude_column: 'column',
        longitude_column: 'column',
      },
      source_requirements: [
        {
          id: 'latitude_column',
          mode: 'column',
          param: 'latitude_column',
          label: 'Latitude column',
          min: 1,
          accepted_column_types: ['integer', 'number', 'text'],
        },
        {
          id: 'longitude_column',
          mode: 'column',
          param: 'longitude_column',
          label: 'Longitude column',
          min: 1,
          accepted_column_types: ['integer', 'number', 'text'],
        },
      ],
      logical_outputs: [{ key: 'geo_point', column_type: 'geo_point' }],
    },
  }) as GeneratedActionCatalogEntry;
}

function generatedTemplate(entry: GeneratedActionCatalogEntry): ActionTemplate {
  const catalog = completeCatalogPayload([entry]);
  const template = actionTemplatesFromCatalog(catalog)
    .find((candidate) => candidate.actionKind === entry.kind);
  if (!template) throw new Error(`Missing generated template for ${entry.kind}`);
  return template;
}

beforeEach(() => {
  requestSequence = 0;
  vi.stubGlobal('crypto', { randomUUID: () => `generated-request-${++requestSequence}` });
});

afterEach(() => {
  cleanup();
  stores?.dispose();
  stores = null;
  vi.unstubAllGlobals();
});

describe('GeneratedActionForm', () => {
  it('uses resolved materialization for target controls without interpreting Params names', async () => {
    const entry = syntheticActionCatalogEntry('example.capture', {
      input_schema: { type: 'object', required: ['destination'], properties: {
        destination: { type: 'string', enum: ['column', 'child'], default: 'column' },
      } },
      row_scope_policy: { kind: 'sheet_rows', selectors: ['all_rows', 'exact_membership'] },
      ui_hints: { form: 'generated', semantic_controls: {}, logical_outputs: [],
        dynamic_outputs: true, typed_action: { creates_sheet: false } },
    }) as GeneratedActionCatalogEntry;
    const resolveParams = vi.fn(async ({ params }: { params: Record<string, unknown> }) => ({
      diagnostics: {}, creates_sheet: params.destination === 'child',
      logical_outputs: params.destination === 'child'
        ? [{ key: 'url', column_type: 'link' }]
        : [{ key: 'page', column_type: 'file' }],
    }));
    const onExecute = vi.fn();
    render(<GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
      sheet={SHEET} running={false} resolveParams={resolveParams}
      selectedRowIds={['21']} onExecute={onExecute} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(screen.queryByTestId('field-sheet_name')).not.toBeInTheDocument();
    expect(screen.getByTestId('field-output-page')).toHaveValue('page');
    fireEvent.change(screen.getByTestId('field-destination'), { target: { value: 'child' } });
    await waitFor(() => expect(screen.getByTestId('field-sheet_name')).toBeInTheDocument());
    expect(screen.queryByTestId('field-output-page')).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId('field-sheet_name'), { target: { value: 'Collected links' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls.at(-1)![0]).toMatchObject({
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [21] },
      params: { destination: 'child' }, sheet_name: 'Collected links', output_names: {},
    });
    fireEvent.change(screen.getByTestId('field-destination'), { target: { value: 'column' } });
    await waitFor(() => expect(screen.queryByTestId('field-sheet_name')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls.at(-1)![0]).not.toHaveProperty('sheet_name');
    expect(onExecute.mock.calls.at(-1)![0].output_names).toEqual({ page: 'page' });
  });

  it.each([
    { label: 'omitted optional defaults', params: { caption: 'Rows' } },
    { label: 'explicit null, false and zero', params: {
      caption: 'Rows', options: null, enabled: false, limit: 0,
    } },
  ])('preserves supplied create-sheet Params with $label', async ({ params }) => {
    const entry = syntheticActionCatalogEntry('example.table', {
      input_schema: {
        type: 'object', required: ['caption'], properties: {
          caption: { type: 'string' },
          options: { anyOf: [{ type: 'object' }, { type: 'null' }] },
          enabled: { type: 'boolean', default: true },
          limit: { type: 'integer', default: 10 },
        },
      },
      ui_hints: { form: 'generated', semantic_controls: {}, logical_outputs: [],
        dynamic_outputs: true, typed_action: { creates_sheet: true } },
    }) as GeneratedActionCatalogEntry;
    const onExecute = vi.fn();
    render(<GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
      sheet={SHEET} running={false} resolveParams={resolveStaticParams}
      initialDraft={{ action_id: entry.kind, scope: { kind: 'project' },
        sheet_name: 'Rows', params, output_names: {} }}
      onExecute={onExecute} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId('run-button')).toBeEnabled());
    fireEvent.click(screen.getByTestId('run-button'));
    expect(onExecute.mock.calls[0][0].params).toEqual(params);
  });

  it('uses the create-sheet marker for project scope, naming and unresolved output renames', async () => {
    const entry = syntheticActionCatalogEntry('example.table', {
      title: 'Create a discovered table',
      input_schema: {
        type: 'object', required: ['caption'],
        properties: { caption: { type: 'string' } },
      },
      row_scope_policy: { kind: 'project', selectors: [] },
      ui_hints: {
        form: 'generated', category: 'convert', semantic_controls: {},
        logical_outputs: [], dynamic_outputs: true,
        typed_action: { creates_sheet: true },
      },
    }) as GeneratedActionCatalogEntry;
    const onExecute = vi.fn();
    const resolveParams = vi.fn(resolveStaticParams);
    render(
      <GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
        sheet={SHEET} selectedRowIds={['2']} running={false}
        initialDraft={{ action_id: entry.kind, scope: { kind: 'project' },
          sheet_name: 'Saved table', params: { caption: 'Rows' }, output_names: {} }}
        resolveParams={resolveParams} onExecute={onExecute} onClose={vi.fn()} />,
    );
    expect(screen.getByTestId('field-sheet_name')).toHaveValue('Saved table');
    await waitFor(() => expect(resolveParams).toHaveBeenCalledWith({
      action_id: 'example.table', scope: { kind: 'project' }, params: { caption: 'Rows' },
    }));
    expect(screen.getByTestId('generated-action-preview')).toBeEnabled();
    expect(screen.queryByTestId('generated-action-run-scope-menu-button')).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId('field-sheet_name'), { target: { value: ' ' } });
    expect(screen.getByTestId('run-button')).toBeDisabled();
    expect(screen.getByTestId('generated-action-preview')).toBeDisabled();
    fireEvent.change(screen.getByTestId('field-sheet_name'), { target: { value: ' Result ' } });
    await waitFor(() => expect(screen.getByTestId('run-button')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-preview'));
    expect(onExecute).toHaveBeenNthCalledWith(1, {
      action_id: 'example.table', scope: { kind: 'project' }, sheet_name: 'Result',
      params: { caption: 'Rows' }, output_names: {},
      idempotency_key: 'web-example.table:generated-request-1',
    }, 'preview');
    fireEvent.click(screen.getByTestId('run-button'));
    expect(onExecute).toHaveBeenCalledWith({
      action_id: 'example.table', scope: { kind: 'project' }, sheet_name: 'Result',
      params: { caption: 'Rows' }, output_names: {},
      idempotency_key: 'web-example.table:generated-request-2',
    }, 'run');
    expect(onExecute.mock.calls[0][0]).not.toHaveProperty('row_ids');
  });

  it.each([
    { label: 'a blank output key', outputs: [{ key: '', column_type: 'text' }] },
    { label: 'duplicate output keys', outputs: [
      { key: 'result', column_type: 'text' },
      { key: 'result', column_type: 'text' },
    ] },
  ])('refuses a static generated catalog entry with $label', ({ outputs }) => {
    const entry = generatedEntry('map.template');
    expect(isGeneratedActionCatalogEntry({
      ...entry,
      ui_hints: { ...entry.ui_hints, logical_outputs: outputs },
    })).toBe(false);
  });

  it('accepts a generated action that writes existing cells without new output columns', () => {
    const entry = generatedEntry('map.template');
    expect(isGeneratedActionCatalogEntry({
      ...entry,
      ui_hints: { ...entry.ui_hints, logical_outputs: [] },
    })).toBe(true);
  });

  it('always asks the server to validate static typed Params and gates invalid fields', async () => {
    const entry = generatedEntry('map.template');
    const resolveParams = vi.fn(async () => ({
      diagnostics: { template: { ok: false, message: 'Unknown template column.' } },
      logical_outputs: [],
    }));
    const onExecute = vi.fn();
    render(
      <GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
        sheet={SHEET} initialSourceColumn="name" running={false}
        resolveParams={resolveParams} onExecute={onExecute} onClose={vi.fn()} />,
    );

    await waitFor(() => expect(resolveParams).toHaveBeenCalledWith({
      action_id: 'map.template',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { template: { text: '{{name}}' } },
    }));
    expect(await screen.findByTestId('field-template-error'))
      .toHaveTextContent('Unknown template column.');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('opens an unlisted generated action without a frontend ID registration', async () => {
    const entry = {
      ...generatedEntry('map.template'),
      kind: 'map.decorate',
      title: 'Decorate text',
    };
    const catalog = completeCatalogPayload([entry]);
    stores = createWorkspaceStores('project-one', createProjectApi('project-one'));
    stores.actionCatalog.store.set(() => ({
      status: 'ready',
      error: null,
      catalog,
      resolvedTemplates: actionTemplatesFromCatalog(catalog),
      version: 1,
    }));

    render(
      <WorkspaceStoresContext.Provider value={stores}>
        <ActionPanel
          sheet={SHEET}
          running={false}
          routeActionKind="map.decorate"
          routeActionLaunchId={1}
          onRun={vi.fn()}
          onExecuteRegisteredAction={vi.fn()}
        />
      </WorkspaceStoresContext.Provider>,
    );

    expect(await screen.findByTestId('generated-action-form')).toBeInTheDocument();
    expect(screen.getByTestId('action-form-title')).toHaveTextContent('Decorate text');
  });

  it('keeps a direct launcher route authoritative while a proposal briefly coexists', async () => {
    const template = generatedEntry('map.template');
    const decorate = {
      ...template,
      kind: 'map.decorate',
      title: 'Decorate text',
    };
    const catalog = completeCatalogPayload([template, decorate]);
    stores = createWorkspaceStores('project-one', createProjectApi('project-one'));
    stores.actionCatalog.store.set(() => ({
      status: 'ready',
      error: null,
      catalog,
      resolvedTemplates: actionTemplatesFromCatalog(catalog),
      version: 1,
    }));

    render(
      <WorkspaceStoresContext.Provider value={stores}>
        <ActionPanel
          sheet={SHEET}
          running={false}
          routeActionKind="map.decorate"
          routeActionLaunchId={4}
          inspectProposal={{
            seq: 3,
            title: 'Saved greeting',
            spec: {
              action_id: 'map.template',
              scope: { kind: 'sheet_rows', sheet_id: 7 },
              params: { template: { text: 'Hi {{name}}' } },
              output_names: { rendered: 'salutation' },
            },
          }}
          onRun={vi.fn()}
          onExecuteRegisteredAction={vi.fn()}
        />
      </WorkspaceStoresContext.Provider>,
    );

    expect(await screen.findByTestId('action-form-title')).toHaveTextContent('Decorate text');
    expect(screen.getByTestId('field-template')).not.toHaveValue('Hi {{name}}');
  });

  it('is selected by ActionPanel for an exact generated catalog entry', async () => {
    const entry = generatedEntry('map.template');
    const catalog = completeCatalogPayload([entry]);
    const onExecute = vi.fn();
    stores = createWorkspaceStores('project-one', createProjectApi('project-one'));
    stores.actionCatalog.store.set(() => ({
      status: 'ready',
      error: null,
      catalog,
      resolvedTemplates: actionTemplatesFromCatalog(catalog),
      version: 1,
    }));

    render(
      <WorkspaceStoresContext.Provider value={stores}>
        <ActionPanel
          sheet={SHEET}
          running={false}
          routeActionKind="map.template"
          routeActionLaunchId={1}
          routeActionInitial={{ sourceColumn: 'name' }}
          onRun={vi.fn()}
          onExecuteRegisteredAction={onExecute}
        />
      </WorkspaceStoresContext.Provider>,
    );

    expect(await screen.findByTestId('generated-action-form')).toBeInTheDocument();
    expect(screen.getByTestId('field-template')).toHaveValue('{{name}}');
    fireEvent.change(screen.getByTestId('field-template'), {
      target: { value: 'Keep {{name}}' },
    });
    act(() => stores?.actionCatalog.store.set((current) => ({
      ...current,
      status: 'loading',
      catalog: null,
      resolvedTemplates: [],
    })));
    expect(screen.getByTestId('generated-action-form')).toBeInTheDocument();
    expect(screen.getByTestId('field-template')).toHaveValue('Keep {{name}}');
    expect(screen.getByTestId('generated-action-preview')).toBeDisabled();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('inspects a Copilot draft with its exact params, outputs, and row scope', async () => {
    const entry = generatedEntry('map.template');
    const catalog = completeCatalogPayload([entry]);
    const onRun = vi.fn();
    stores = createWorkspaceStores('project-one', createProjectApi('project-one'));
    vi.spyOn(stores.projectApi, 'resolveActionParams').mockImplementation(resolveStaticParams);
    stores.actionCatalog.store.set(() => ({
      status: 'ready',
      error: null,
      catalog,
      resolvedTemplates: actionTemplatesFromCatalog(catalog),
      version: 1,
    }));

    render(
      <WorkspaceStoresContext.Provider value={stores}>
        <ActionPanel
          sheet={SHEET}
          running={false}
          inspectProposal={{
            seq: 2,
            title: 'Saved greeting',
            spec: {
              action_id: 'map.template',
              scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [2] },
              params: { template: { text: 'Hi {{name}}' } },
              output_names: { rendered: 'salutation' },
            },
          }}
          onRun={vi.fn()}
          onExecuteRegisteredAction={onRun}
        />
      </WorkspaceStoresContext.Provider>,
    );

    expect(await screen.findByTestId('field-template')).toHaveValue('Hi {{name}}');
    expect(screen.getByTestId('action-form-title')).toHaveTextContent('Saved greeting');
    expect(screen.getByTestId('field-output-rendered')).toHaveValue('salutation');
    expect(screen.getByText('1 selected rows will run')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onRun).toHaveBeenCalledWith({
      action_id: 'map.template',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [2] },
      params: { template: { text: 'Hi {{name}}' } },
      output_names: { rendered: 'salutation' },
      idempotency_key: 'web-map.template:generated-request-1',
    }, 'run');
  });

  it('refuses a Copilot draft whose output keys differ from its catalog', async () => {
    const entry = generatedEntry('map.template');
    const catalog = completeCatalogPayload([entry]);
    stores = createWorkspaceStores('project-one', createProjectApi('project-one'));
    stores.actionCatalog.store.set(() => ({
      status: 'ready',
      error: null,
      catalog,
      resolvedTemplates: actionTemplatesFromCatalog(catalog),
      version: 1,
    }));

    render(
      <WorkspaceStoresContext.Provider value={stores}>
        <ActionPanel
          sheet={SHEET}
          running={false}
          inspectProposal={{
            seq: 3,
            title: 'Bad greeting',
            spec: {
              action_id: 'map.template',
              scope: { kind: 'sheet_rows', sheet_id: 7 },
              params: { template: { text: 'Hi {{name}}' } },
              output_names: { wrong: 'salutation' },
            },
          }}
          onRun={vi.fn()}
          onExecuteRegisteredAction={vi.fn()}
        />
      </WorkspaceStoresContext.Provider>,
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(SAVED_ACTION_SPEC_REFUSAL);
    expect(screen.queryByTestId('generated-action-form')).not.toBeInTheDocument();
  });

  it('builds one canonical template request for Preview and Run without legacy fields', async () => {
    const onPreview = vi.fn();
    const onRun = vi.fn();
    render(
      <GeneratedActionForm
        catalogEntry={generatedEntry('map.template')}
        actionTemplate={generatedTemplate(generatedEntry('map.template'))}
        sheet={SHEET}
        selectedRowIds={['2', '3']}
        initialSourceColumn="name"
        running={false}
        resolveParams={resolveStaticParams}
        onExecute={(request, intent) => (intent === 'preview' ? onPreview : onRun)(request)}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByTestId('field-template')).toHaveValue('{{name}}');

    fireEvent.change(screen.getByTestId('field-template'), {
      target: { value: 'Hello {{name}}' },
    });
    fireEvent.change(screen.getByTestId('field-output-rendered'), {
      target: { value: ' greeting ' },
    });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-preview'));
    fireEvent.click(screen.getByTestId('generated-action-run'));

    const expected = {
      action_id: 'map.template',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [2, 3] },
      params: { template: { text: 'Hello {{name}}' } },
      output_names: { rendered: 'greeting' },
      idempotency_key: 'web-map.template:generated-request-1',
    };
    expect(onPreview).toHaveBeenCalledWith(expected);
    expect(onRun).toHaveBeenCalledWith({
      ...expected,
      idempotency_key: 'web-map.template:generated-request-2',
    });
    expect(onRun.mock.calls[0][0]).not.toHaveProperty('actionKind');
    expect(onRun.mock.calls[0][0]).not.toHaveProperty('canonicalAction');
    expect(onRun.mock.calls[0][0]).not.toHaveProperty('previewRows');
  });

  it('uses authoritative dynamic outputs for regex naming and execution', async () => {
    const entry = generatedEntry('map.regex_extract');
    const resolveParams = vi.fn(async ({ params }: { params: Record<string, unknown> }) => (
      params.pattern === '('
        ? {
            diagnostics: { pattern: { ok: false, message: 'unterminated subpattern' } },
            logical_outputs: [],
          }
        : {
            diagnostics: {},
            logical_outputs: String(params.pattern).includes('(b)')
              ? [
                  { key: 'extracted_1', column_type: 'text' },
                  { key: 'extracted_2', column_type: 'text' },
                ]
              : [{ key: 'extracted', column_type: 'text' }],
          }
    ));
    const onRun = vi.fn();
    render(
      <GeneratedActionForm
        catalogEntry={entry}
        actionTemplate={generatedTemplate(entry)}
        sheet={SHEET}
        initialDraft={{
          action_id: entry.kind,
          scope: { kind: 'sheet_rows', sheet_id: 7 },
          params: {
            input_columns: ['raw'], pattern: '(a)(b)', all_matches: false,
            group: null, timeout_seconds: 0.05,
          },
          output_names: { extracted_1: 'first', extracted_2: 'second' },
        }}
        running={false}
        resolveParams={resolveParams}
        onExecute={(request, intent) => intent === 'run' && onRun(request)}
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByTestId('field-output-extracted_1')).toHaveValue('first');
    expect(screen.getByTestId('field-output-extracted_2')).toHaveValue('second');
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
      action_id: 'map.regex_extract',
      params: expect.objectContaining({ input_columns: ['raw'], pattern: '(a)(b)' }),
      output_names: { extracted_1: 'first', extracted_2: 'second' },
    }));

    fireEvent.change(screen.getByTestId('field-pattern'), { target: { value: '(' } });
    await waitFor(() => expect(screen.getByTestId('field-pattern-error')).toHaveTextContent(
      'unterminated subpattern',
    ));
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  });

  it.each([
    {
      label: 'regex',
      kind: 'map.regex_extract' as const,
      params: {
        input_columns: ['raw'], pattern: '(a)', all_matches: false,
        group: null, timeout_seconds: 0.05,
      },
      saved: { stale: 'old_result' },
      outputs: [{ key: 'extracted', column_type: 'text' }],
    },
    {
      label: 'columns from JSON',
      kind: 'map.columns_from_json' as const,
      params: {
        source_column: 'payload', routes: [{ name: 'city', path: '$.city' }],
      },
      saved: { old_city: 'location' },
      outputs: [{ key: 'city', column_type: 'json' }],
    },
  ])('refuses stale $label saved output keys after dynamic resolution', async ({
    kind, params, saved, outputs,
  }) => {
    const entry = generatedEntry(kind);
    const onExecute = vi.fn();
    render(
      <GeneratedActionForm
        catalogEntry={entry}
        actionTemplate={generatedTemplate(entry)}
        sheet={kind === 'map.columns_from_json'
          ? sheetMeta([
              columnDef({ id: '11', name: 'payload', type: 'json' }),
            ], { id: '7', name: 'Data', rowCount: 2 })
          : SHEET}
        initialDraft={{
          action_id: kind,
          scope: { kind: 'sheet_rows', sheet_id: 7 },
          params,
          output_names: saved,
        }}
        running={false}
        resolveParams={vi.fn(async () => ({ diagnostics: {}, logical_outputs: outputs }))}
        onExecute={onExecute}
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(
      'Saved action outputs no longer match its parameters.',
    ));
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  it.each([{}, { extracted_1: 'first' }])('defaults omitted dynamic output renames in saved drafts: %j', async (saved) => {
    const entry = generatedEntry('map.regex_extract');
    const onExecute = vi.fn();
    render(<GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
      sheet={SHEET} running={false} initialDraft={{
        action_id: 'map.regex_extract', scope: { kind: 'sheet_rows', sheet_id: 7 },
        params: { input_columns: ['raw'], pattern: '(a)(b)', all_matches: false,
          group: null, timeout_seconds: 0.05 },
        output_names: saved,
      }} resolveParams={vi.fn(async () => ({ diagnostics: {}, logical_outputs: [
        { key: 'extracted_1', column_type: 'text' },
        { key: 'extracted_2', column_type: 'text' },
      ] }))} onExecute={onExecute} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({ output_names: {
      extracted_1: 'extracted_1', extracted_2: 'extracted_2', ...saved,
    } }), 'run');
  });

  it.each([{}, { rendered: 'chosen_name' }])('keeps saved static output targets despite existing-name collisions: %j', async (saved) => {
    const entry = generatedEntry('map.template');
    const onExecute = vi.fn();
    const target = saved.rendered ?? 'rendered';
    const sheet = sheetMeta([
      ...SHEET.columns,
      columnDef({ id: '15', name: 'rendered', type: 'text' }),
      columnDef({ id: '16', name: 'chosen_name', type: 'text' }),
    ], { id: '7', rowCount: 2 });
    render(<GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
      sheet={sheet} running={false} initialDraft={{
        action_id: 'map.template', scope: { kind: 'sheet_rows', sheet_id: 7 },
        params: { template: { text: '{{name}}' } }, output_names: saved,
      }} resolveParams={resolveStaticParams} onExecute={onExecute} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId('field-output-rendered')).toHaveValue(target));
    // Existing source columns are a visible collision, not permission to
    // rename the saved target or overwrite source data.
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeDisabled());
    expect(screen.queryByTestId('default-output-name-collision-rendered')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('explains fresh deduped defaults while retaining the logical name through recomputation', async () => {
    const entry = generatedEntry('map.clean_dates');
    const collidingSheet = sheetMeta([
      ...SHEET.columns,
      ...['raw_iso', 'raw_iso_2', 'name_iso', 'name_iso_2'].map((name, index) => (
        columnDef({ id: String(20 + index), name, type: 'date' })
      )),
    ], { id: '7', rowCount: 2 });
    render(<GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
      sheet={collidingSheet} running={false} resolveParams={resolveStaticParams}
      onExecute={vi.fn()} onClose={vi.fn()} />);

    await waitFor(() => expect(screen.getByTestId('field-output-cleaned')).toHaveValue('raw_iso_3'));
    expect(screen.getByTestId('default-output-name-collision-cleaned'))
      .toHaveTextContent('A "raw_iso" column already exists — saving to "raw_iso_3".');

    fireEvent.change(screen.getByTestId('field-source'), { target: { value: 'name' } });
    await waitFor(() => expect(screen.getByTestId('field-output-cleaned')).toHaveValue('name_iso_3'));
    expect(screen.getByTestId('default-output-name-collision-cleaned'))
      .toHaveTextContent('A "name_iso" column already exists — saving to "name_iso_3".');

    fireEvent.change(screen.getByTestId('field-output-cleaned'), { target: { value: 'normalized_date' } });
    expect(screen.queryByTestId('default-output-name-collision-cleaned')).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId('field-source'), { target: { value: 'raw' } });
    await waitFor(() => expect(screen.getByTestId('field-output-cleaned')).toHaveValue('normalized_date'));
    expect(screen.queryByTestId('default-output-name-collision-cleaned')).not.toBeInTheDocument();
  });

  it.each([
    { label: 'a resolved dynamic output', outputs: [{ key: 'extracted', column_type: 'text' }], count: '1 output column' },
    { label: 'an unresolved dynamic output shape', outputs: [], count: null },
  ])('only reports a known output-column count for $label', async ({ outputs, count }) => {
    const entry = generatedEntry('map.regex_extract');
    const estimateAction = vi.fn(async () => ({ cost: 0.01, rows: 1, billed_cost: 1_000 }));
    render(<GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
      sheet={SHEET} running={false} initialDraft={{
        action_id: entry.kind, scope: { kind: 'sheet_rows', sheet_id: 7 },
        params: { input_columns: ['raw'], pattern: '(value)', all_matches: false,
          group: null, timeout_seconds: 0.05 }, output_names: {},
      }} resolveParams={vi.fn(async () => ({ diagnostics: {}, logical_outputs: outputs }))}
      estimateAction={estimateAction}
      onExecute={vi.fn()} onClose={vi.fn()} />);

    await waitFor(() => expect(estimateAction).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId('cost-estimate')).toHaveTextContent(/\b1 row\b/));
    const line = screen.getByTestId('cost-estimate');
    expect(line).toHaveTextContent(/\b1 row\b/);
    if (count) expect(line).toHaveTextContent(/\b1 output column\b/);
    else expect(line).not.toHaveTextContent(/output column/);
  });

  it('does not retain an old dynamic output width while resolving changed parameters', async () => {
    const entry = generatedEntry('map.regex_extract');
    let resolveSecond: ((resolution: ActionParamResolution) => void) | undefined;
    let resolutionCount = 0;
    const resolveParams = vi.fn(() => {
      resolutionCount += 1;
      if (resolutionCount === 1) return Promise.resolve({ diagnostics: {}, logical_outputs: [
        { key: 'extracted', column_type: 'text' },
      ] });
      return new Promise<ActionParamResolution>((resolve) => { resolveSecond = resolve; });
    });
    render(<GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
      sheet={SHEET} running={false} initialDraft={{
        action_id: entry.kind, scope: { kind: 'sheet_rows', sheet_id: 7 },
        params: { input_columns: ['raw'], pattern: '(value)', all_matches: false,
          group: null, timeout_seconds: 0.05 }, output_names: {},
      }} resolveParams={resolveParams}
      estimateAction={vi.fn(async () => ({ cost: 0.01, rows: 1, billed_cost: 1_000 }))}
      onExecute={vi.fn()} onClose={vi.fn()} />);

    await waitFor(() => expect(screen.getByTestId('cost-estimate')).toHaveTextContent(/\b1 output column\b/));
    fireEvent.change(screen.getByTestId('field-pattern'), { target: { value: '(value)(other)' } });
    await waitFor(() => expect(resolveSecond).toBeDefined());
    expect(screen.getByTestId('cost-estimate')).not.toHaveTextContent(/output column/);

    resolveSecond!({ diagnostics: {}, logical_outputs: [
      { key: 'extracted_1', column_type: 'text' },
      { key: 'extracted_2', column_type: 'text' },
    ] });
    await waitFor(() => expect(screen.getByTestId('cost-estimate')).toHaveTextContent('2 output columns'));
  });

  it('keeps a saved clean-dates logical target instead of applying fresh-name defaults', async () => {
    const entry = generatedEntry('map.clean_dates');
    const onExecute = vi.fn();
    render(<GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
      sheet={SHEET} running={false} initialDraft={{
        action_id: 'map.clean_dates', scope: { kind: 'sheet_rows', sheet_id: 7 },
        params: { source: 'raw' }, output_names: {},
      }} resolveParams={resolveStaticParams} onExecute={onExecute} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(screen.getByTestId('field-output-cleaned')).toHaveValue('cleaned');
    fireEvent.change(screen.getByTestId('field-source'), { target: { value: 'name' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(screen.getByTestId('field-output-cleaned')).toHaveValue('cleaned');
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      params: { source: 'name' }, output_names: { cleaned: 'cleaned' },
    }), 'run');
  });

  it('lets an edited saved action adopt a newly resolved dynamic output shape', async () => {
    const entry = generatedEntry('map.regex_extract');
    const onExecute = vi.fn();
    render(
      <GeneratedActionForm
        catalogEntry={entry}
        actionTemplate={generatedTemplate(entry)}
        sheet={SHEET}
        initialDraft={{
          action_id: entry.kind,
          scope: { kind: 'sheet_rows', sheet_id: 7 },
          params: {
            input_columns: ['raw'], pattern: '(a)', all_matches: false,
            group: null, timeout_seconds: 0.05,
          },
          output_names: { stale: 'old_result' },
        }}
        running={false}
        resolveParams={vi.fn(async ({ params }) => ({
          diagnostics: {},
          logical_outputs: params.pattern === '(a)(b)'
            ? [
                { key: 'extracted_1', column_type: 'text' },
                { key: 'extracted_2', column_type: 'text' },
              ]
            : [{ key: 'extracted', column_type: 'text' }],
        }))}
        onExecute={onExecute}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(
      'Saved action outputs no longer match its parameters.',
    ));
    fireEvent.change(screen.getByTestId('field-pattern'), { target: { value: '(a)(b)' } });
    await waitFor(() => expect(screen.getByTestId('field-output-extracted_2')).toBeEnabled());
    expect(screen.getByTestId('generated-action-run')).toBeEnabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      params: expect.objectContaining({ pattern: '(a)(b)' }),
      output_names: { extracted_1: 'extracted_1', extracted_2: 'extracted_2' },
    }), 'run');
  });

  it('keeps invalid numeric text as an editable draft and does not submit it', async () => {
    const entry = generatedEntry('map.regex_extract');
    const onExecute = vi.fn();
    render(
      <GeneratedActionForm
        catalogEntry={entry}
        actionTemplate={generatedTemplate(entry)}
        sheet={SHEET}
        initialDraft={{
          action_id: entry.kind,
          scope: { kind: 'sheet_rows', sheet_id: 7 },
          params: {
            input_columns: ['raw'], pattern: '(a)', all_matches: false,
            group: null, timeout_seconds: 0.05,
          },
          output_names: { extracted: 'extracted' },
        }}
        running={false}
        resolveParams={vi.fn(async () => ({
          diagnostics: {}, logical_outputs: [{ key: 'extracted', column_type: 'text' }],
        }))}
        onExecute={onExecute}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(() => fireEvent.change(screen.getByTestId('field-timeout_seconds'), {
      target: { value: 'not-a-number' },
    })).not.toThrow();
    expect(screen.getByTestId('field-timeout_seconds')).toHaveValue('not-a-number');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(() => fireEvent.click(screen.getByTestId('generated-action-run'))).not.toThrow();
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('offers compatible generated columns without carrying replacement consent', async () => {
    const entry = generatedEntry('map.template');
    const onExecute = vi.fn();
    const sheet = sheetMeta([
      ...SHEET.columns,
      columnDef({ id: '13', name: 'generated_text', type: 'text',
        ai: aiMeta(), generationManaged: true }),
      columnDef({ id: '14', name: 'generated_number', type: 'number',
        ai: aiMeta(), generationManaged: true }),
    ], { id: '7', name: 'People', rowCount: 2 });
    render(
      <GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
        sheet={sheet} initialSourceColumn="name" running={false}
        resolveParams={resolveStaticParams}
        onExecute={onExecute} onClose={vi.fn()} />,
    );

    fireEvent.focus(screen.getByTestId('field-output-rendered'));
    expect(screen.getByTestId('field-output-rendered-option-generated-text'))
      .toBeInTheDocument();
    expect(screen.queryByTestId('field-output-rendered-option-generated-number'))
      .not.toBeInTheDocument();
    expect(screen.queryByTestId('field-output-rendered-option-raw')).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId('field-output-rendered'), {
      target: { value: 'generated_text' },
    });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      output_names: { rendered: 'generated_text' },
    }), 'run');
    expect(onExecute.mock.calls[0][0]).not.toHaveProperty('replace_existing');
  });

  it('offers plain compatible targets only when the resolved output policy allows them', async () => {
    const entry = generatedEntry('map.template');
    entry.ui_hints.dynamic_outputs = true;
    const onExecute = vi.fn();
    render(
      <GeneratedActionForm catalogEntry={entry} actionTemplate={generatedTemplate(entry)}
        sheet={SHEET} initialSourceColumn="name" running={false}
        resolveParams={async () => ({ diagnostics: {}, logical_outputs: [{
          key: 'rendered', column_type: 'text', existing_column_policy: 'compatible',
        }] })}
        onExecute={onExecute} onClose={vi.fn()} />,
    );
    const target = await screen.findByTestId('field-output-rendered');
    fireEvent.focus(target);
    expect(screen.getByTestId('field-output-rendered-option-raw')).toBeInTheDocument();
    fireEvent.change(target, { target: { value: 'raw' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      output_names: { rendered: 'raw' },
    }), 'run');
    expect(onExecute.mock.calls[0][0]).not.toHaveProperty('replace_existing');
  });

  it('keeps exact-empty scope, switching, backfill, and native submission in the host', async () => {
    const entry = generatedEntry('map.template');
    const template = generatedTemplate(entry);
    const alternate = { ...template, kind: 'decorate', name: 'Decorate' } as ActionTemplate;
    const onExecute = vi.fn();
    const onSwitchAction = vi.fn();
    const onBackfill = vi.fn();
    const sheet = sheetMeta([
      ...SHEET.columns,
      columnDef({ id: '13', name: 'generated_text', type: 'text', generationManaged: true,
        ai: aiMeta({ versions: [{ runId: 'run-1', version: 1, model: 'test',
          startedAt: '2026-09-05T00:00:00Z', rowCount: 1, cost: 0, status: 'partial' }] }) }),
    ], { id: '7', name: 'People', rowCount: 2 });
    render(
      <GeneratedActionForm catalogEntry={entry} actionTemplate={template} sheet={sheet}
        selectedRowIds={[]} hasExactRowScopeInitializer initialSourceColumn="name"
        initialDraft={{ action_id: 'map.template',
          scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [] },
          params: { template: { text: '{{name}}' } }, output_names: { rendered: 'generated_text' } }}
        running={false} resolveParams={resolveStaticParams} onExecute={onExecute}
        switchActions={[template, alternate]} onSwitchAction={onSwitchAction}
        onBackfill={onBackfill} onClose={vi.fn()} />,
    );

    expect(screen.getByTestId('generated-action-form').tagName).toBe('FORM');
    expect(screen.getByText('0 selected rows will run')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.submit(screen.getByTestId('generated-action-form'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [] },
    }), 'run');

    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    fireEvent.click(screen.getByTestId('generated-action-row-scope-all'));
    expect(screen.getByTestId('generated-action-run')).toBeEnabled();
    fireEvent.submit(screen.getByTestId('generated-action-form'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      scope: { kind: 'sheet_rows', sheet_id: 7 },
    }), 'run');

    fireEvent.click(screen.getByTestId('action-title-menu-button'));
    fireEvent.click(screen.getByTestId('action-switch-decorate'));
    expect(onSwitchAction).toHaveBeenCalledWith(alternate);

    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    fireEvent.click(screen.getByTestId('generated-action-row-scope-backfill'));
    expect(onBackfill).toHaveBeenCalledWith('generated_text');
  });

  it('generates clean-dates controls and keeps their canonical values', async () => {
    const onRun = vi.fn();
    render(
      <GeneratedActionForm
        catalogEntry={generatedEntry('map.clean_dates')}
        actionTemplate={generatedTemplate(generatedEntry('map.clean_dates'))}
        sheet={SHEET}
        running={false}
        resolveParams={resolveStaticParams}
        onExecute={(request, intent) => {
          if (intent === 'run') onRun(request);
        }}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByTestId('field-source')).toHaveValue('raw');
    expect(screen.getByTestId('clean-dates-format-select')).toHaveValue('auto');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onRun).toHaveBeenLastCalledWith({
      action_id: 'map.clean_dates',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'raw', format: null },
      output_names: { cleaned: 'raw_iso' },
      idempotency_key: 'web-map.clean_dates:generated-request-1',
    });

    fireEvent.change(screen.getByTestId('field-source'), { target: { value: 'name' } });
    fireEvent.change(screen.getByTestId('clean-dates-format-select'), {
      target: { value: '%Y-%m-%d' },
    });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));

    expect(onRun).toHaveBeenLastCalledWith({
      action_id: 'map.clean_dates',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'name', format: '%Y-%m-%d' },
      output_names: { cleaned: 'name_iso' },
      idempotency_key: 'web-map.clean_dates:generated-request-2',
    });
  });

  it('generates compact clean-column controls and emits typed values', async () => {
    const entry = generatedEntry('map.clean_column');
    const onRun = vi.fn();
    render(
      <GeneratedActionForm
        catalogEntry={entry}
        actionTemplate={generatedTemplate(entry)}
        sheet={SHEET}
        initialSourceColumn="name"
        running={false}
        resolveParams={resolveStaticParams}
        onExecute={(request, intent) => {
          if (intent === 'run') onRun(request);
        }}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByLabelText('Column to clean')).toHaveValue('name');
    expect(screen.getByTestId('field-case')).toHaveValue('smart_title');
    expect(screen.getByText('Fix capitalization (names & titles)')).toBeInTheDocument();
    const cleanings = screen.getByTestId('cleanings-grid');
    expect(cleanings).toHaveTextContent('Cleanings');
    expect(cleanings).toHaveTextContent('Normalize US phone');
    expect(screen.getByTestId('field-blank_null_tokens')).toBeChecked();
    expect(screen.getByTestId('field-strip_edge_punct')).not.toBeChecked();
    expect(screen.getByText('Blank these tokens')).toBeInTheDocument();
    expect(screen.getByTestId('field-null_tokens')).toHaveValue('n/a,na,null,none,unknown');

    fireEvent.change(screen.getByTestId('field-case'), { target: { value: 'lower' } });
    fireEvent.click(screen.getByTestId('field-strip_edge_punct'));
    fireEvent.click(screen.getByTestId('field-blank_null_tokens'));
    expect(screen.getByTestId('field-null_tokens')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));

    expect(onRun).toHaveBeenCalledWith({
      action_id: 'map.clean_column',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: {
        source: 'name',
        case: 'lower',
        blank_null_tokens: false,
        null_tokens: 'n/a,na,null,none,unknown',
        lowercase_emails: true,
        normalize_us_phone: true,
        expand_abbreviations: true,
        reorder_person_name: true,
        canonicalize_duplicates: true,
        strip_edge_punct: true,
        normalize_unicode_punct: false,
        remove_thousands_separators: false,
        remove_all_commas: false,
        make_numeric: false,
      },
      output_names: { cleaned: 'name_clean' },
      idempotency_key: 'web-map.clean_column:generated-request-1',
    });
  });

  it('filters and independently seeds geo-point columns, then emits its registered request', async () => {
    const onRun = vi.fn();
    const sheet = sheetMeta([
      columnDef({ id: '11', name: 'active', type: 'boolean' }),
      columnDef({ id: '12', name: 'population', type: 'integer' }),
      columnDef({ id: '13', name: 'Longitude', type: 'number' }),
      columnDef({ id: '14', name: 'Latitude', type: 'text' }),
      columnDef({ id: '15', name: 'location', type: 'geo_point' }),
    ], { id: '7', name: 'Places', rowCount: 2 });
    const entry = generatedEntry('map.to_geo_point');

    render(
      <GeneratedActionForm
        catalogEntry={entry}
        actionTemplate={generatedTemplate(entry)}
        sheet={sheet}
        running={false}
        resolveParams={resolveStaticParams}
        onExecute={(request, intent) => {
          if (intent === 'run') onRun(request);
        }}
        onClose={vi.fn()}
      />,
    );

    const latitude = screen.getByTestId('field-latitude_column');
    const longitude = screen.getByTestId('field-longitude_column');
    expect(latitude).toHaveValue('Latitude');
    expect(longitude).toHaveValue('Longitude');
    expect(Array.from((latitude as HTMLSelectElement).options).map((option) => option.value))
      .toEqual(['population', 'Longitude', 'Latitude']);
    expect(screen.getByTestId('field-output-geo_point')).toHaveValue('geo_point');

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onRun).toHaveBeenCalledWith({
      action_id: 'map.to_geo_point',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: {
        latitude_column: 'Latitude',
        longitude_column: 'Longitude',
      },
      output_names: { geo_point: 'geo_point' },
      idempotency_key: 'web-map.to_geo_point:generated-request-1',
    });
  });

  it('uses the server model diagnostic to refuse the same source for both geo-point roles', async () => {
    const onRun = vi.fn();
    const entry = generatedEntry('map.to_geo_point');
    const sheet = sheetMeta([
      columnDef({ id: '11', name: 'lat', type: 'number' }),
      columnDef({ id: '12', name: 'lon', type: 'number' }),
    ], { id: '7', name: 'Places', rowCount: 2 });

    render(
      <GeneratedActionForm
        catalogEntry={entry}
        actionTemplate={generatedTemplate(entry)}
        sheet={sheet}
        initialDraft={{
          action_id: 'map.to_geo_point',
          scope: { kind: 'sheet_rows', sheet_id: 7 },
          params: { latitude_column: 'lat', longitude_column: 'lat' },
          output_names: { geo_point: 'location' },
        }}
        running={false}
        resolveParams={vi.fn(async ({ params }) => ({
          diagnostics: params.latitude_column === params.longitude_column
            ? { __all__: { ok: false,
                message: 'latitude and longitude must come from different columns' } }
            : {},
          logical_outputs: [],
        }))}
        onExecute={onRun}
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'latitude and longitude must come from different columns',
    );
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onRun).not.toHaveBeenCalled();
  });

});
