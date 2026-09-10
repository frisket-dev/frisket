// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import type { GeneratedActionDraft, SheetMeta } from '../../src/api/types';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { servedActionCatalog } from '../support/servedActionCatalog';
const catalog = servedActionCatalog();

const sheet: SheetMeta = { id: '7', name: 'Stories', rowCount: 3,
  columns: [{ id: '11', name: 'story', type: 'text' }, { id: '12', name: 'category', type: 'category' }],
  citedColumnIds: [], annotatedTextColumnIds: [] };
let stores: WorkspaceStores | undefined;
afterEach(() => { cleanup(); stores?.dispose(); stores = undefined; vi.unstubAllGlobals(); });

const examples: Array<[string, GeneratedActionDraft['params'], Record<string, string>, boolean?]> = [
  ['map.classify', { source: ['story'], engine: 'local_semantic',
    fields: [{ name: 'category', type: 'category', labels: ['yes', 'no'] }] }, { category: 'classification' }],
  ['map.extract', { source: ['story'], model: 'test/model',
    fields: [{ name: 'value', type: 'text' }] }, { value: 'extracted' }],
  ['map.ner', { source: ['story'], engine: 'spacy', labels: ['person'] }, { entities: 'entities' }],
  ['map.ner', { source: ['story'], engine: 'llm', model: 'test/model', labels: ['person'],
    extra_instructions: 'Prefer public agencies.\nKeep exact source spelling.' }, { entities: 'entities' }],
  ['map.translate', { source: ['story'], engine: 'llm', model: 'test/model', target_language: 'French' },
    { translation: 'translated' }],
  ['research.answer', { source: ['story'], model: 'test/model', question: { text: 'Verify {{story}}' },
    include_sources: true }, { answer: 'answer', sources: 'answer_sources' }],
  ['map.mcp_extract', { source: ['story'], model: 'test/model', mcp_server_ids: ['server-one'],
    fields: [{ name: 'value', type: 'text' }] }, { value: 'tool_result' }],
  ['reduce.group_summary', { source: ['story'], group_by: 'category', model: 'test/model', instruction: 'Summarize.' }, {}, true],
  ['map.find', { source: 'story', model: 'test/model', instruction: 'Find statements.', fields: [] }, {}, true],
];

it.each(examples)('submits %s through the generated form using served Params and request scope', async (kind, params, output_names, createsSheet) => {
  const entry = structuredClone(catalog.actions.find((item) => item.kind === kind));
  if (!entry || !isGeneratedActionCatalogEntry(entry)) throw new Error(`${kind} is not generated`);
  entry.ui_hints.engines = entry.ui_hints.engines?.map((engine) => ({ ...engine, available: true }));
  const template = generatedActionTemplateFromCatalogEntry(entry);
  if (!template) throw new Error('Missing template');
  const api = createProjectApi('typed-forms');
  vi.spyOn(api, 'listMcpServers').mockResolvedValue([]);
  stores = createWorkspaceStores('typed-forms', api);
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No provider request in this test')));
  const onExecute = vi.fn();
  const draft: GeneratedActionDraft = { action_id: kind, scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [2] },
    params, output_names, ...(createsSheet ? { sheet_name: 'Results' } : {}) };
  render(<WorkspaceStoresContext.Provider value={stores}><GeneratedActionForm
    catalogEntry={entry} actionTemplate={template} sheet={sheet} initialDraft={draft}
    selectedRowIds={['2']} hasExactRowScopeInitializer running={false}
    resolveParams={async () => ({ diagnostics: {}, creates_sheet: createsSheet ?? false,
      logical_outputs: Object.keys(output_names).map((key) => ({ key, column_type: 'text' })) })}
    onExecute={onExecute} onClose={vi.fn()} /></WorkspaceStoresContext.Provider>);
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  const guidanceFields: Record<string, string[]> = {
    'map.classify': ['context'], 'map.extract': ['instruction', 'context'],
    'map.mcp_extract': ['instruction', 'context'], 'map.find': ['instruction'],
    'map.translate': ['context'], 'map.ner': params.engine === 'llm' ? ['extra_instructions'] : [],
  };
  for (const name of guidanceFields[kind] ?? []) {
    expect(screen.getByTestId(`field-${name}`).tagName).toBe('TEXTAREA');
  }
  fireEvent.click(screen.getByTestId('generated-action-run'));
  if (kind === 'reduce.group_summary') {
    expect(onExecute).toHaveBeenCalledWith({
      ...draft,
      output_names: {},
      idempotency_key: expect.stringMatching(/^web-reduce\.group_summary:/),
    }, 'run');
  } else {
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining(draft), 'run');
  }
  expect(onExecute.mock.lastCall?.[0]).not.toHaveProperty('canonicalAction');
});

it('preserves a question redirected to Extract as the output field instruction', async () => {
  const entry = catalog.actions.find((item) => item.kind === 'map.extract');
  if (!entry || !isGeneratedActionCatalogEntry(entry)) throw new Error('Missing Extract');
  const template = generatedActionTemplateFromCatalogEntry(entry)!;
  const resolveParams = vi.fn(async () => ({ diagnostics: {}, logical_outputs: [] }));
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No provider request in this test')));
  render(<GeneratedActionForm catalogEntry={entry} actionTemplate={template} sheet={sheet}
    initialSourceColumn="story" initialPrompt="Who signed the contract?" running={false}
    resolveParams={resolveParams} onExecute={vi.fn()} onClose={vi.fn()} />);
  await waitFor(() => expect(resolveParams).toHaveBeenCalledWith(expect.objectContaining({
    params: expect.objectContaining({ source: ['story'],
      fields: [{ name: 'who_signed_the_contract', type: 'text', description: 'Who signed the contract?' }] }),
  })));
  expect(screen.getByLabelText('Field 1 description')).toHaveValue('Who signed the contract?');
});
