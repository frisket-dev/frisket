// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry, type SheetMeta } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { servedActionCatalog } from '../support/servedActionCatalog';

const catalog = servedActionCatalog();
let stores: WorkspaceStores | undefined;
afterEach(() => { cleanup(); stores?.dispose(); stores = undefined; vi.unstubAllGlobals(); });

it.each(['map.ask', 'map.summarize', 'map.extract', 'map.ner'])('%s follows its declared PDF or text source contract', (kind) => {
  const entry = catalog.actions.find((item) => item.kind === kind);
  if (!entry || !isGeneratedActionCatalogEntry(entry)) throw new Error('Missing generated action');
  const template = generatedActionTemplateFromCatalogEntry(entry);
  if (!template) throw new Error('Missing template');
  const sheet: SheetMeta = { id: '7', name: 'Documents', rowCount: 1,
    columns: [{ id: '1', name: 'body', type: 'text' }, { id: '2', name: 'picture', type: 'image' },
      { id: '3', name: 'document', type: 'file' }], citedColumnIds: [], annotatedTextColumnIds: [] };
  stores = createWorkspaceStores('files', createProjectApi('files'));
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No remote calls')));
  render(<WorkspaceStoresContext.Provider value={stores}><GeneratedActionForm
    catalogEntry={entry} actionTemplate={template} sheet={sheet} selectedRowIds={[]} running={false}
    initialDraft={{ action_id: kind, scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: ['body'], model: 'openai/gpt-5-mini', ...(kind === 'map.ask' ? { question: 'What?' }
        : kind === 'map.ner' ? { engine: 'llm', labels: ['PERSON'] }
          : kind === 'map.extract' ? { engine: 'llm', fields: [{ name: 'fact', type: 'text' }] } : {}) }, output_names: {} }}
    resolveParams={vi.fn(async () => ({ diagnostics: {}, logical_outputs: entry.ui_hints.logical_outputs }))}
    onExecute={vi.fn()} onClose={vi.fn()} /></WorkspaceStoresContext.Provider>);
  const acceptsPdf = kind !== 'map.ner';
  if (acceptsPdf) expect(screen.queryByText('For file contents, run To markdown or OCR first, then select the resulting text column.')).not.toBeInTheDocument();
  else expect(screen.getByText('For file contents, run To markdown or OCR first, then select the resulting text column.')).toBeVisible();
  fireEvent.mouseDown(screen.getByTestId('text-source-columns'));
  const menu = screen.getByRole('listbox');
  expect(within(menu).getByText('body')).toBeVisible();
  if (kind === 'map.ner') expect(within(menu).queryByText('picture')).not.toBeInTheDocument();
  else expect(within(menu).getByText('picture')).toBeVisible();
  if (acceptsPdf) expect(within(menu).getByText('document')).toBeVisible();
  else expect(within(menu).queryByText('document')).not.toBeInTheDocument();
  fireEvent.click(screen.getByTestId('text-source-mode-template'));
  const insert = screen.getByTestId('text-source-template-column-insert');
  expect(within(insert).getByRole('option', { name: 'body' })).toBeInTheDocument();
  expect(within(insert).getByRole('option', { name: 'picture' })).toBeInTheDocument();
  if (acceptsPdf) expect(within(insert).getByRole('option', { name: 'document' })).toBeInTheDocument();
  else expect(within(insert).queryByRole('option', { name: 'document' })).not.toBeInTheDocument();
});
