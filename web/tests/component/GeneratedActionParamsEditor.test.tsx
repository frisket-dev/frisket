// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import * as api from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';
import type {
  GeneratedActionCatalogEntry,
  GeneratedActionDraft,
  SheetMeta,
} from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { GeneratedActionParamsEditor } from '../../src/components/action-panel/GeneratedActionForm';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { columnDef } from '../support/domainFixtures';
import { installActionSelectorFixture } from '../support/selectorChoicesFixture';

const SHEET: SheetMeta = {
  id: '7',
  name: 'Court filings',
  rowCount: 240,
  columns: [
    columnDef({ id: '1', name: 'story', type: 'text' }),
    columnDef({ id: '2', name: 'notes', type: 'text' }),
  ],
};

const ENTRY = {
  kind: 'map.extract',
  authoring_contract_version: 1,
  title: 'Extract structured fields',
  description: 'Extract structured fields from each row.',
  input_schema: {
    type: 'object',
    required: ['source', 'engine', 'fields'],
    properties: {
      source: { type: 'array', title: 'Source', items: { type: 'string' } },
      engine: { type: 'string', title: 'Model', default: 'llm' },
      model: { type: ['string', 'null'], title: 'Model', default: null },
      fields: { type: 'array', title: 'Fields', items: { type: 'object' } },
      instruction: { type: 'string', title: 'Instructions', default: '' },
      grounding: { type: ['object', 'null'], title: 'Grounding', default: null },
      evidence_policy: { type: ['object', 'null'], title: 'Evidence policy', default: null },
      source_document_columns: { type: 'array', title: 'Citation sources', items: { type: 'string' }, default: [] },
      context: { type: 'string', title: 'Context', default: '' },
      include_confidence: { type: 'boolean', title: 'Include confidence', default: false },
    },
  },
  output_schema: { type: 'object' },
  errors: [],
  side_effects: [],
  required_capabilities: ['model:complete'],
  row_scope_policy: { kind: 'sheet_rows', selectors: ['all_rows', 'exact_membership'] },
  required_credentials: [],
  cost_policy: { kind: 'none' },
  idempotency: { kind: 'required' },
  retry_policy: { kind: 'safe' },
  execution_mode: 'sync',
  async_mode: 'inline',
  writes_project: true,
  examples: [],
  ui_hints: {
    form: 'generated',
    category: 'extract',
    uses_model: true,
    semantic_controls: {
      source: 'rich_source',
      engine: 'engine',
      model: 'model',
      source_document_columns: 'columns',
    },
    source_requirements: [{
      id: 'source', mode: 'columns', param: 'source', label: 'Source', min: 1,
      accepted_column_types: ['text'], template_accepted_column_types: ['text'],
    }],
    engines: [{ id: 'llm', label: 'Language model', available: true }],
    logical_outputs: [],
    dynamic_outputs: true,
    comparison: { kind: 'row_overlay' },
  },
  receipt_policy: 'writes_receipt',
} as GeneratedActionCatalogEntry;

const DRAFT: GeneratedActionDraft = {
  action_id: 'map.extract',
  scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [11, 12, 13] },
  params: {
    source: ['story'],
    engine: 'llm',
    model: 'test/model',
    instruction: 'Extract the named party.',
    fields: [{ name: 'party', type: 'text', description: 'Party named in the filing' }],
    grounding: { enabled: true, citation_required: true },
    evidence_policy: { citation_required: true },
  },
  output_names: { party: 'pdf_party' },
};

let stores: WorkspaceStores | null = null;

beforeEach(() => {
  vi.spyOn(api, 'listProviders').mockResolvedValue({
    schemaVersion: 'frisket.providers.v1',
    tier: 'local',
    providers: [{
      id: 'test', label: 'Test', kind: 'platform_api', configured: true,
      source: 'env', hint: null,
      models: [{ id: 'test/model', label: 'Test model', price: null }],
    }],
  });
});

afterEach(() => {
  cleanup();
  stores?.dispose();
  stores = null;
  vi.restoreAllMocks();
});

function mountEditor(
  onDraftChange = vi.fn(),
  lockedParams: readonly string[] = ['source', 'fields'],
  options: { entry?: GeneratedActionCatalogEntry; draft?: GeneratedActionDraft } = {},
) {
  const entry = structuredClone(options.entry ?? ENTRY);
  const draft = structuredClone(options.draft ?? DRAFT);
  installActionSelectorFixture(entry);
  const actionTemplate = generatedActionTemplateFromCatalogEntry(entry);
  if (!actionTemplate) throw new Error('map.extract has no generated form');
  const projectApi = createProjectApi('action-compare-editor');
  stores = createWorkspaceStores('action-compare-editor', projectApi);
  render(
    <WorkspaceStoresContext.Provider value={stores}>
      <GeneratedActionParamsEditor
        projectId="action-compare-editor"
        catalogEntry={entry}
        actionTemplate={actionTemplate}
        sheet={SHEET}
        draft={draft}
        lockedParams={lockedParams}
        structureLocked
        lockReason="Fixed for this comparison."
        resolveParams={async (request) => ({
          diagnostics: {},
          logical_outputs: Array.isArray(request.params.fields)
            ? request.params.fields.flatMap((field) => field && typeof field === 'object'
              && !Array.isArray(field) && typeof field.name === 'string'
              ? [{ key: field.name, column_type: String(field.type ?? 'text') }]
              : [])
            : [],
        })}
        onDraftChange={onDraftChange}
      />
    </WorkspaceStoresContext.Provider>,
  );
  return onDraftChange;
}

function mountFinalForm(
  kind: 'edit-existing' | 'use-comparison-variant' | 'use-new-comparison-variant',
  capturedSelectedRowIds: readonly string[] = ['11', '12', '13'],
) {
  const entry = structuredClone(ENTRY);
  installActionSelectorFixture(entry);
  const actionTemplate = generatedActionTemplateFromCatalogEntry(entry);
  if (!actionTemplate) throw new Error('map.extract has no generated form');
  const projectApi = createProjectApi(`action-compare-final:${kind}`);
  stores = createWorkspaceStores(`action-compare-final:${kind}`, projectApi);
  const onCompare = vi.fn();
  const onExecute = vi.fn();
  const onReturnToCompare = vi.fn();
  const onOpenHistory = vi.fn();
  render(
    <WorkspaceStoresContext.Provider value={stores}>
      <GeneratedActionForm
        projectId={`action-compare-final:${kind}`}
        catalogEntry={entry}
        actionTemplate={actionTemplate}
        sheet={SHEET}
        selectedRowIds={['99']}
        initialDraft={DRAFT}
        initializeSelectorDefaults={false}
        editorPolicy={{ lockedParams: ['source', 'fields'], structureLocked: true }}
        launchMode={{
          ...(kind === 'use-new-comparison-variant' ? { kind } : {
            kind,
            expectedOutputColumns: { pdf_party: 101 },
          }),
          contextLabel: kind === 'edit-existing'
            ? 'on Court filings · settings from run 42'
            : 'on Court filings · from variant B',
          capturedSelectedRowIds,
          comparisonStale: kind === 'use-comparison-variant',
          onReturnToCompare,
        }}
        running={false}
        resolveParams={async (request) => ({
          diagnostics: {},
          logical_outputs: Array.isArray(request.params.fields)
            ? request.params.fields.flatMap((field) => field && typeof field === 'object'
              && !Array.isArray(field) && typeof field.name === 'string'
              ? [{ key: field.name, column_type: String(field.type ?? 'text') }]
              : [])
            : [],
        })}
        onCompare={onCompare}
        onOpenHistory={onOpenHistory}
        onExecute={onExecute}
        onClose={vi.fn()}
      />
    </WorkspaceStoresContext.Provider>,
  );
  return { onCompare, onExecute, onReturnToCompare, onOpenHistory };
}

describe('GeneratedActionParamsEditor', () => {
  it('reuses the normal selector while locking sources and output structure', async () => {
    const onDraftChange = mountEditor();

    expect(screen.getByTestId('generated-action-form')).toHaveAttribute('data-mode', 'params-only');
    expect(screen.queryByTestId('action-form-title')).not.toBeInTheDocument();
    expect(screen.queryByTestId('generated-action-run')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-output-party')).not.toBeInTheDocument();
    expect(screen.getByTestId('field-engine')).toBeInTheDocument();

    expect(screen.queryByTestId('field-source-lock-reason')).not.toBeInTheDocument();
    expect(screen.getByTestId('text-source-mode-columns')).toBeDisabled();
    expect(screen.getByTestId('text-source-mode-template')).toBeDisabled();

    expect(screen.queryByRole('button', { name: 'Add column' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('output-field-delete')).not.toBeInTheDocument();
    expect(screen.getByTestId('output-field-name')).toBeDisabled();
    expect(screen.getByTestId('output-field-type')).toBeDisabled();
    expect(screen.getByTestId('output-field-description')).toBeEnabled();
    expect(screen.queryByTestId('output-structure-lock-hint')).not.toBeInTheDocument();

    await waitFor(() => expect(onDraftChange).toHaveBeenCalled());
    onDraftChange.mockClear();
    fireEvent.change(screen.getByTestId('output-field-description'), {
      target: { value: 'Full legal name of the party' },
    });
    await waitFor(() => expect(onDraftChange).toHaveBeenCalled());
    expect(onDraftChange.mock.lastCall?.[0]).toMatchObject({
      params: {
        source: ['story'],
        fields: [{
          name: 'party',
          type: 'text',
          description: 'Full legal name of the party',
        }],
      },
      output_names: { party: 'pdf_party' },
    });
  });

  it('accepts a Classify description edit from a sorted server draft with omitted defaults', async () => {
    const entry = structuredClone(ENTRY);
    entry.kind = 'map.classify';
    entry.title = 'Classify';
    entry.input_schema.properties = {
      source: { type: 'array', title: 'Source', items: { type: 'string' } },
      engine: { type: 'string', title: 'Model', default: 'llm' },
      model: { type: ['string', 'null'], title: 'Model', default: null },
      fields: { type: 'array', title: 'Fields', items: { type: 'object' } },
      context: { type: 'string', title: 'Context', default: '' },
      include_confidence: { type: 'boolean', title: 'Include confidence', default: false },
      include_justification: { type: 'boolean', title: 'Include justification', default: false },
    };
    entry.ui_hints.engines = [{
      id: 'llm', label: 'Language model', available: true,
      classification_options: {
        field_types: ['category'], max_fields: 1,
        include_confidence: true, include_justification: true,
      },
    }];
    const draft: GeneratedActionDraft = {
      action_id: 'map.classify',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: {
        source: ['story'],
        engine: 'llm',
        model: 'test/model',
        fields: [{
          description: 'Existing topic description',
          label_descriptions: { Housing: 'Housing policy' },
          labels: ['Housing', 'Transit'],
          name: 'topic',
        }],
      },
      output_names: { topic: 'topic' },
    };
    const onDraftChange = mountEditor(vi.fn(), ['source', 'fields'], { entry, draft });
    await waitFor(() => expect(onDraftChange).toHaveBeenCalled());
    onDraftChange.mockClear();

    expect(screen.getByTestId('classify-labels')).toBeDisabled();
    expect(screen.getByLabelText('Housing semantic description')).toBeEnabled();
    fireEvent.change(screen.getByTestId('output-field-description'), {
      target: { value: 'Updated topic description' },
    });

    await waitFor(() => expect(onDraftChange).toHaveBeenCalled());
    expect(screen.queryByTestId('generated-locked-param-problem')).not.toBeInTheDocument();
    expect(onDraftChange.mock.lastCall?.[0]).toMatchObject({
      params: { fields: [{
        name: 'topic', type: 'category', description: 'Updated topic description',
        labels: ['Housing', 'Transit'], label_descriptions: { Housing: 'Housing policy' },
      }] },
    });
  });

  it('keeps Ask response type locked when the comparison context names it', () => {
    const entry = structuredClone(ENTRY);
    entry.kind = 'map.ask';
    entry.title = 'Ask';
    entry.input_schema.required = ['source', 'question', 'response_type'];
    entry.input_schema.properties = {
      source: { type: 'array', title: 'Source', items: { type: 'string' } },
      question: { type: 'string', title: 'Question' },
      response_type: { type: 'string', title: 'Response type', enum: ['text', 'integer'], default: 'text' },
      include_confidence: { type: 'boolean', title: 'Include confidence', default: false },
      include_reasoning: { type: 'boolean', title: 'Include reasoning', default: false },
    };
    entry.ui_hints.semantic_controls = { source: 'rich_source' };
    entry.ui_hints.engines = [];
    const draft: GeneratedActionDraft = {
      action_id: 'map.ask', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: ['story'], question: 'How many?', response_type: 'text' },
      output_names: { answer: 'answer' },
    };

    mountEditor(vi.fn(), ['source', 'response_type'], { entry, draft });

    expect(screen.getByTestId('field-response_type')).toBeDisabled();
  });

  it('refuses an entire custom-body update when it changes a locked top-level param', async () => {
    const onDraftChange = mountEditor(vi.fn(), ['source', 'fields', 'grounding']);
    await waitFor(() => expect(onDraftChange).toHaveBeenCalled());
    onDraftChange.mockClear();

    fireEvent.change(screen.getByTestId('field-citation_mode'), { target: { value: 'none' } });

    expect(await screen.findByTestId('generated-locked-param-problem')).toHaveTextContent(
      '“grounding” cannot be changed here',
    );
    expect(onDraftChange).not.toHaveBeenCalled();
  });

  it('keeps Compare and final replacement scopes distinct for an existing action', async () => {
    const { onCompare, onExecute, onOpenHistory } = mountFinalForm('edit-existing');
    expect(screen.getByText('on Court filings · settings from run 42')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('action-history-button'));
    expect(onOpenHistory).toHaveBeenCalledTimes(1);

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(screen.getByTestId('generated-action-run')).toHaveTextContent('Compare');
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onCompare).toHaveBeenCalledWith(expect.objectContaining({
      action_id: 'map.extract',
      output_names: { party: 'pdf_party' },
    }));
    expect(onExecute).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    expect(screen.getByTestId('generated-action-row-scope-all')).toHaveTextContent('Replace all rows');
    expect(screen.getByTestId('generated-action-row-scope-selected')).toHaveTextContent(
      'Replace selected rows',
    );
    fireEvent.click(screen.getByTestId('generated-action-row-scope-selected'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [11, 12, 13] },
      replace_existing: true,
      expected_output_columns: { pdf_party: 101 },
    }), 'run');
  });

  it('keeps replacement scope truthful and reversible when a selected-row launch is cancelled', async () => {
    const { onExecute, onReturnToCompare } = mountFinalForm('use-comparison-variant');
    expect(screen.getByText('on Court filings · from variant B')).toBeInTheDocument();
    expect(screen.getByTestId('comparison-stale-run-notice')).toHaveTextContent(
      'This run will use current data',
    );
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(screen.getByTestId('generated-action-run')).toHaveTextContent('Replace all rows');
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      replace_existing: true,
      expected_output_columns: { pdf_party: 101 },
    }), 'run');

    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    fireEvent.click(screen.getByTestId('generated-action-row-scope-selected'));
    expect(onExecute.mock.lastCall?.[0]).toMatchObject({
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [11, 12, 13] },
    });

    // A cost-gate cancellation leaves this form mounted. Its next action and
    // summary must describe the retained selected scope, and all rows must
    // remain reachable without reopening the comparison.
    expect(screen.getByTestId('generated-action-run')).toHaveTextContent('Replace selected rows');
    expect(screen.getByText('3 selected rows will be replaced')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    fireEvent.click(screen.getByTestId('generated-action-row-scope-all'));
    expect(onExecute.mock.lastCall?.[0]).toMatchObject({
      scope: { kind: 'sheet_rows', sheet_id: 7 },
    });
    expect(screen.getByTestId('generated-action-run')).toHaveTextContent('Replace all rows');

    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    fireEvent.click(screen.getByTestId('generated-action-menu-return-to-compare'));
    expect(onReturnToCompare).toHaveBeenCalledTimes(1);
  });

  it('hides selected-row execution when the captured comparison has no selection', async () => {
    mountFinalForm('use-comparison-variant', []);
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());

    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));

    expect(screen.queryByTestId('generated-action-row-scope-selected')).not.toBeInTheDocument();
  });

  it('keeps a chosen fresh-draft variant as a normal all-row Run', async () => {
    const { onExecute } = mountFinalForm('use-new-comparison-variant');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(screen.getByTestId('generated-action-run')).toHaveTextContent('Run');
    expect(screen.getByText('Run defaults to all 240 rows')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      scope: { kind: 'sheet_rows', sheet_id: 7 },
    }), 'run');
    expect(onExecute.mock.lastCall?.[0]).not.toHaveProperty('replace_existing');
    expect(onExecute.mock.lastCall?.[0]).not.toHaveProperty('expected_output_columns');
  });

  it('adds Compare to the ordinary Run menu without adding replacement intent', async () => {
    const entry = structuredClone(ENTRY);
    installActionSelectorFixture(entry);
    const actionTemplate = generatedActionTemplateFromCatalogEntry(entry);
    if (!actionTemplate) throw new Error('map.extract has no generated form');
    const projectApi = createProjectApi('action-compare-new');
    stores = createWorkspaceStores('action-compare-new', projectApi);
    const onCompare = vi.fn();
    const onExecute = vi.fn();
    render(
      <WorkspaceStoresContext.Provider value={stores}>
        <GeneratedActionForm projectId="action-compare-new" catalogEntry={entry}
          actionTemplate={actionTemplate} sheet={SHEET} initialDraft={DRAFT}
          running={false} resolveParams={async () => ({ diagnostics: {}, logical_outputs: [
            { key: 'party', column_type: 'text' },
          ] })} onCompare={onCompare} onExecute={onExecute} onClose={vi.fn()} />
      </WorkspaceStoresContext.Provider>,
    );

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    fireEvent.click(screen.getByTestId('generated-action-menu-compare'));
    expect(onCompare).toHaveBeenCalledWith(expect.objectContaining({
      action_id: 'map.extract',
      output_names: { party: 'pdf_party' },
    }));
    expect(onCompare.mock.lastCall?.[0]).not.toHaveProperty('replace_existing');
    expect(onExecute).not.toHaveBeenCalled();
  });
});
