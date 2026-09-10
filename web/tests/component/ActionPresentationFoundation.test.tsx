// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import {
  actionTemplatesFromCatalog,
  generatedActionTemplateFromCatalogEntry,
} from '../../src/actions/model';
import type {
  ActionParam,
  ActionTemplate,
  GeneratedActionCatalogEntry,
  GeneratedActionDraft,
} from '../../src/api/types';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import {
  buildCanonicalDraft,
  GenericSchemaRenderer,
  isClosedVocabularyControl,
  resolveActionPresentation,
  serializeCanonicalDraft,
  setCanonicalDraftField,
  supportsGenericActionTemplate,
} from '../../src/components/action-panel/actionPresentation';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { columnDef } from '../support/domainFixtures';
import { completeCatalogPayload, sheetMeta } from '../support/actionFormFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { servedActionCatalog } from '../support/servedActionCatalog';

let stores: WorkspaceStores | undefined;

beforeAll(installPopoverPolyfill);
afterEach(() => {
  cleanup();
  stores?.dispose();
  stores = undefined;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function chooseEngine(engineId: string): void {
  fireEvent.click(screen.getByTestId('model-picker-button'));
  fireEvent.change(screen.getByTestId('model-picker-search'), {
    target: { value: `engine:${engineId}` },
  });
  fireEvent.click(screen.getByTestId(`model-option-engine-${engineId.replace(/_/g, '-')}`));
}

const catalog = completeCatalogPayload();
const templates = actionTemplatesFromCatalog(catalog);
const servedCatalog = servedActionCatalog();

function templateFor(canonicalKind: string): ActionTemplate {
  const template = templates.find((candidate) => candidate.actionKind === canonicalKind);
  if (!template) throw new Error(`Missing fixture template: ${canonicalKind}`);
  return template;
}

/** The backend-served entry for a typed model action, with every engine
 * marked available so the tiered picker offers each one. */
function servedTypedEntry(kind: string): GeneratedActionCatalogEntry {
  const entry = structuredClone(servedCatalog.actions.find((candidate) => candidate.kind === kind));
  if (!entry || !isGeneratedActionCatalogEntry(entry)) throw new Error(`${kind} is not generated`);
  entry.ui_hints.engines = entry.ui_hints.engines?.map((engine) => ({ ...engine, available: true }));
  return entry;
}

const TYPED_SHEET = sheetMeta([columnDef({ id: '11', name: 'source', type: 'text' })], { id: '7' });

/** Mount the generated form for one served typed entry the way ActionPanel
 * does for a 'generated' disposition; the emitted request is the typed
 * envelope (action_id/scope/params/output_names/idempotency_key). */
function renderTypedForm(
  entry: GeneratedActionCatalogEntry,
  params: GeneratedActionDraft['params'],
  outputNames: Record<string, string>,
) {
  const template = generatedActionTemplateFromCatalogEntry(entry);
  if (!template) throw new Error(`Missing generated template for ${entry.kind}`);
  expect(resolveActionPresentation(template)).toEqual({ kind: 'generated' });
  const api = createProjectApi('presentation-foundation');
  vi.spyOn(api, 'listMcpServers').mockResolvedValue([]);
  stores = createWorkspaceStores('presentation-foundation', api);
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No provider request in this test')));
  const onExecute = vi.fn();
  const draft: GeneratedActionDraft = {
    action_id: entry.kind,
    scope: { kind: 'sheet_rows', sheet_id: 7 },
    params,
    output_names: outputNames,
  };
  const resolveParams = async () => ({
    diagnostics: {},
    logical_outputs: Object.keys(outputNames).map((key) => ({ key, column_type: 'text' })),
  });
  const onClose = vi.fn();
  // One tree builder so a parent rerender re-mounts NOTHING: the same entry,
  // template, draft, and callbacks arrive again, exactly as ActionPanel
  // re-renders the mounted typed form on unrelated state changes.
  const tree = () => (
    <WorkspaceStoresContext.Provider value={stores}>
      <GeneratedActionForm
        catalogEntry={entry}
        actionTemplate={template}
        sheet={TYPED_SHEET}
        initialDraft={draft}
        running={false}
        resolveParams={resolveParams}
        onExecute={onExecute}
        onClose={onClose}
      />
    </WorkspaceStoresContext.Provider>
  );
  const rendered = render(tree());
  const rerenderTypedForm = () => rendered.rerender(tree());
  return { ...rendered, onExecute, rerenderTypedForm };
}

function renderServedTranslate() {
  const entry = servedTypedEntry('map.translate');
  // Only the INSTALLED-pair fact is a test input; the pair roster
  // (downloadable_pairs) stays exactly as the backend serves it.
  entry.ui_hints.engines = entry.ui_hints.engines?.map((engine) => (
    engine.id === 'opus_mt' ? { ...engine, models: ['en-es'] } : engine
  ));
  return renderTypedForm(
    entry,
    { source: ['source'], target_language: 'French' },
    { translation: 'translation' },
  );
}

describe('canonical action draft', () => {
  it('refuses to resolve a template without its catalog authoring version', () => {
    const template = { ...templateFor('resolve.fill_missing') };
    delete (template as ActionTemplate & { authoringContractVersion?: 0 | 1 })
      .authoringContractVersion;

    expect(() => resolveActionPresentation(template)).toThrow();
  });

  it('keeps defaults absent while preserving explicit empty and false values', () => {
    const template = templateFor('resolve.fill_missing');
    const untouched = buildCanonicalDraft(template);

    expect(untouched).not.toHaveProperty('method');
    expect(untouched).not.toHaveProperty('treat_blank_as_missing');
    expect(serializeCanonicalDraft(untouched)).toEqual({});

    const edited = buildCanonicalDraft(template, {
      fill_value: '',
      treat_blank_as_missing: false,
    });
    expect(edited).toEqual({ fill_value: '', treat_blank_as_missing: false });
    expect(typeof edited.treat_blank_as_missing).toBe('boolean');
    expect(() => buildCanonicalDraft(template, { treat_blank_as_missing: 'False' })).toThrow(
      'Canonical boolean field treat_blank_as_missing must be true or false',
    );
  });

  it('updates only declared fields', () => {
    const draft = buildCanonicalDraft(templateFor('resolve.fill_missing'));
    const template = templateFor('resolve.fill_missing');
    expect(setCanonicalDraftField(template, draft, 'fill_value', '')).toEqual({ fill_value: '' });
    expect(() => setCanonicalDraftField(template, draft, 'unknown', 'x')).toThrow(
      'Unknown canonical action field: unknown',
    );
  });
});

describe('closed generic schema renderer', () => {
  it('renders schema defaults without marking them present and emits edits', () => {
    const template = templateFor('resolve.fill_missing');
    const draft = buildCanonicalDraft(template);
    const onFieldChange = vi.fn();
    render(
      <GenericSchemaRenderer
        template={template}
        draft={draft}
        columns={[]}
        onFieldChange={onFieldChange}
      />,
    );

    expect(screen.getByTestId('field-method')).toHaveValue('down');
    expect(screen.getByTestId('field-treat_blank_as_missing')).toBeChecked();
    expect(screen.queryByTestId('field-fill_value')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('field-treat_blank_as_missing'));
    expect(onFieldChange).toHaveBeenCalledWith('treat_blank_as_missing', false);
  });

  it('validates visibility against displayed canonical values', () => {
    const template = templateFor('resolve.fill_missing');
    const draft = buildCanonicalDraft(template, { method: 'down' });
    render(
      <GenericSchemaRenderer
        template={template}
        draft={draft}
        columns={[]}
        onFieldChange={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('field-fill_value')).not.toBeInTheDocument();
  });

  it('accepts a catalog-driven column-select control', () => {
    const template = templateFor('resolve.fill_missing');
    const widened: ActionTemplate = {
      ...template,
      params: [...(template.params ?? []), {
        name: 'secondary_source',
        label: 'Secondary source',
        input: 'column',
      }],
    };
    expect(supportsGenericActionTemplate(widened)).toBe(true);
    expect(resolveActionPresentation(widened)).toEqual({ kind: 'generated' });
  });

  it('keeps the generated fill controls inside the generic vocabulary', () => {
    const template = templateFor('resolve.fill_missing');
    expect(supportsGenericActionTemplate(template)).toBe(true);
    expect(resolveActionPresentation(template)).toEqual({ kind: 'generated' });
  });

  it('admits aiGeneratedOnly only for a column-select control', () => {
    const template = templateFor('resolve.fill_missing');
    const columnTemplate: ActionTemplate = {
      ...template,
      params: [...(template.params ?? []), {
        name: 'judged_column',
        label: 'Answer to grade',
        input: 'column',
        aiGeneratedOnly: true,
      }],
    };
    const textTemplate: ActionTemplate = {
      ...template,
      params: [...(template.params ?? []), {
        name: 'judged_column',
        label: 'Answer to grade',
        input: 'text',
        aiGeneratedOnly: true,
      }],
    };

    expect(supportsGenericActionTemplate(columnTemplate)).toBe(true);
    expect(resolveActionPresentation(columnTemplate)).toEqual({ kind: 'generated' });
    expect(supportsGenericActionTemplate(textTemplate)).toBe(false);
  });

  it('WEB-06-B kind-free resolver pin: admits a columns control with no kind name in the input', () => {
    const columnsParam: ActionParam = {
      name: 'sources',
      label: 'Sources',
      input: 'columns',
    };
    const namelessTemplate = { params: [columnsParam] } as ActionTemplate;

    expect('kind' in namelessTemplate).toBe(false);
    expect('actionKind' in namelessTemplate).toBe(false);
    expect(isClosedVocabularyControl(columnsParam)).toBe(true);
    expect(supportsGenericActionTemplate(namelessTemplate)).toBe(true);
  });

  it('ACTION-07-F2F admits and renders a kind-free optional-column control', () => {
    const optionalColumnParam: ActionParam = {
      name: 'group_key',
      label: 'Group by',
      input: 'column-optional',
    };
    const namelessTemplate = { params: [optionalColumnParam] } as ActionTemplate;

    expect('kind' in namelessTemplate).toBe(false);
    expect('actionKind' in namelessTemplate).toBe(false);
    expect(isClosedVocabularyControl(optionalColumnParam)).toBe(true);
    expect(supportsGenericActionTemplate(namelessTemplate)).toBe(true);

    render(
      <GenericSchemaRenderer
        template={namelessTemplate}
        draft={{}}
        columns={[columnDef({ id: 'beat', name: 'beat', type: 'category' })]}
        onFieldChange={vi.fn()}
      />,
    );
    expect(screen.getByTestId('field-group_key')).toHaveValue('');
    expect(screen.getByTestId('field-group_key')).toHaveTextContent('— all rows —');
  });

  it('classify delegates its conditional engine/model controls to the shared pickers', async () => {
    // No bespoke binding owns classify's engine/model: the served entry
    // renders through the generated form, whose semantic controls mount the
    // shared EnginePicker and (only for the LLM engine) ModelPicker.
    const entry = servedTypedEntry('map.classify');
    expect(entry.ui_hints.semantic_controls).toMatchObject({ engine: 'engine', model: 'model' });

    const fields = [{ name: 'topic', type: 'category', labels: ['housing', 'transit'] }];
    renderTypedForm(
      entry,
      { source: ['source'], engine: 'llm', model: 'test/model', fields },
      { topic: 'topic' },
    );
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(screen.getByTestId('field-engine-model-choice')).toBeInTheDocument();
    expect(screen.getByTestId('model-picker-button')).toHaveTextContent('test/model');

    chooseEngine('local_semantic');
    expect(screen.getByTestId('model-picker-button')).toHaveTextContent('Local semantic');
    expect(screen.queryByTestId('field-model')).not.toBeInTheDocument();
  });

  it('WEB-06-B extract fields pin: typed form keeps edited fields across parent rerenders', async () => {
    // Extract ships through the served typed form (a 'generated'
    // presentation), not the generic ActionForm; the pin lives on the path
    // users actually edit. The fields editor is uncontrolled by the parent:
    // a rerender with the same served entry, template, and initial draft
    // must keep the user's in-progress column edits, never re-seed them.
    const mounted = renderTypedForm(
      servedTypedEntry('map.extract'),
      {
        source: ['source'],
        model: 'test/model',
        fields: [{ name: 'value', type: 'text', description: '' }],
      },
      { value: 'value' },
    );
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());

    fireEvent.click(screen.getByRole('button', { name: /add column/i }));
    expect(screen.getAllByTestId('output-field-row')).toHaveLength(2);
    const firstName = screen.getAllByTestId('output-field-name')[0];
    const firstType = screen.getAllByTestId('output-field-type')[0];
    const firstDescription = screen.getAllByTestId('output-field-description')[0];
    expect(firstName.tagName).toBe('INPUT');
    expect(firstType).toHaveTextContent('text');
    expect(firstDescription.tagName).toBe('TEXTAREA');

    fireEvent.change(firstName, { target: { value: 'official_name' } });
    fireEvent.change(firstDescription, { target: { value: 'Named official in the source' } });
    expect(firstName).toHaveValue('official_name');
    expect(firstDescription).toHaveValue('Named official in the source');

    mounted.rerenderTypedForm();
    expect(screen.getAllByTestId('output-field-row')).toHaveLength(2);
    expect(screen.getAllByTestId('output-field-name')[0]).toHaveValue('official_name');
    expect(screen.getAllByTestId('output-field-type')[0]).toHaveTextContent('text');
    expect(screen.getAllByTestId('output-field-description')[0]).toHaveValue(
      'Named official in the source',
    );
    // The edited schema is what the typed request carries, by value.
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(mounted.onExecute).toHaveBeenCalledTimes(1);
    expect(mounted.onExecute.mock.lastCall?.[0]).toMatchObject({
      action_id: 'map.extract',
      params: expect.objectContaining({
        fields: expect.arrayContaining([
          expect.objectContaining({
            name: 'official_name',
            type: 'text',
            description: 'Named official in the source',
          }),
        ]),
      }),
    });
  });

  it('STRUCT-W1 control-count pin: renders exactly one target-language control for default and opus_mt engines', async () => {
    const { container } = renderServedTranslate();
    const targetLanguageControls = () => container.querySelectorAll(
      '[data-testid="field-target_language"], [data-testid="translate-pair-target"]',
    );

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(targetLanguageControls()).toHaveLength(1);
    chooseEngine('opus_mt');
    expect(targetLanguageControls()).toHaveLength(1);
  });

  it('STRUCT-W1 emitted-request pin: carries the opus_mt picker target into typed params by value', async () => {
    const { onExecute } = renderServedTranslate();

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    chooseEngine('opus_mt');
    fireEvent.change(screen.getByTestId('translate-pair-source'), { target: { value: 'en' } });
    fireEvent.change(screen.getByTestId('translate-pair-target'), { target: { value: 'es' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));

    expect(onExecute).toHaveBeenCalledTimes(1);
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      action_id: 'map.translate',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: ['source'], engine: 'opus_mt', language: ['en'], target_language: 'es' },
      output_names: { translation: 'translation' },
    }), 'run');
    expect(onExecute.mock.calls[0][0]).not.toHaveProperty('canonicalAction');
  });
});
