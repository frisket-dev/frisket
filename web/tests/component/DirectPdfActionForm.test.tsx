// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry, type GeneratedActionDraft } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { installActionSelectorFixture, selectorProviderCatalog } from '../support/selectorChoicesFixture';

beforeAll(installPopoverPolyfill);
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const MODEL = 'openai/pdf-test';
const catalog = servedActionCatalog();
const entry = catalog.actions.find((item) => item.kind === 'map.ask');
if (!entry || !isGeneratedActionCatalogEntry(entry)) throw new Error('Missing Ask catalog entry');
const template = generatedActionTemplateFromCatalogEntry(entry)!;
const sheet = sheetMeta([columnDef({ id: '1', name: 'document', type: 'file' }),
  columnDef({ id: '2', name: 'notes', type: 'text' })], { id: '7', rowCount: 1 });
function mount(source?: GeneratedActionDraft['params']['source'], blockPdf = false) {
  const selector = installActionSelectorFixture(entry, async () => selectorProviderCatalog([MODEL]));
  const original = selector.getMockImplementation()!;
  selector.mockImplementation(async (...args) => {
    const response = await original(...args);
    response.depends_on = ['source', 'source_contains_pdf'];
    const subject = args[1].subject;
    for (const choice of response.groups.flatMap((group) => group.choices)) {
      choice.facts.push({ kind: 'text', label: 'PDF input', value: blockPdf ? 'Not supported' : 'Supported' });
      if (blockPdf && subject.kind === 'action' && subject.params?.source_contains_pdf === true) {
        choice.status = 'unavailable'; choice.can_author = false; choice.can_run = false;
        choice.blocker = { code: 'pdf_input_unsupported', message: 'This model does not support PDF inputs.', field: null };
      }
    }
    return response;
  });
  const onExecute = vi.fn();
  const resolveParams = vi.fn(async () => ({ diagnostics: {}, logical_outputs: [{ key: 'answer', column_type: 'text' }] }));
  render(<GeneratedActionForm projectId="pdf-form" catalogEntry={entry} actionTemplate={template}
    sheet={sheet} running={false} initialDraft={source === undefined ? undefined : {
      action_id: 'map.ask', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source, model: MODEL, question: 'What does the document say?' }, output_names: { answer: 'Finding' },
    }} resolveParams={resolveParams} onExecute={onExecute} onClose={() => {}} />);
  return { selector, onExecute, resolveParams };
}
it('sends PDF modality only to the model picker while retaining the authored mixed source draft', async () => {
  const { selector, onExecute } = mount(['document', 'notes']);
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  expect(selector.mock.calls.some(([, query]) => query.subject.kind === 'action'
    && query.subject.params?.source_contains_pdf === true)).toBe(true);
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(onExecute).toHaveBeenCalledOnce();
  expect(onExecute.mock.calls[0][0].params.source).toEqual(['document', 'notes']);
  expect(onExecute.mock.calls[0][0].params).not.toHaveProperty('source_contains_pdf');
});
it('refreshes model readiness when a PDF source is added or removed, without changing the saved model', async () => {
  const { selector, onExecute } = mount(['notes'], true);
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.mouseDown(screen.getByTestId('text-source-columns'));
  fireEvent.click(screen.getByRole('option', { name: /^document/ }));
  await waitFor(() => expect(selector.mock.calls.some(([, query]) => query.subject.kind === 'action'
    && query.subject.params?.source_contains_pdf === true)).toBe(true));
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeDisabled());
  expect(screen.getAllByText('This model does not support PDF inputs.').length).toBeGreaterThan(0);
  expect(onExecute).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Remove document' }));
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  const query = selector.mock.calls.at(-1)![1];
  if (query.subject.kind !== 'action') throw new Error('Unexpected selector subject');
  expect(query.subject.params?.source_contains_pdf).toBeUndefined();
  expect(query.subject.params?.model).toBe(MODEL);
});
it('keeps fresh sources empty even when the sheet contains a PDF file column', async () => {
  const { selector, onExecute } = mount();
  await waitFor(() => expect(selector).toHaveBeenCalled());
  expect(screen.queryByRole('button', { name: 'Remove document' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Remove notes' })).not.toBeInTheDocument();
  for (const [, query] of selector.mock.calls) {
    if (query.subject.kind !== 'action') throw new Error('Unexpected selector subject');
    expect(query.subject.params?.source).toEqual([]);
    expect(query.subject.params?.source_contains_pdf).toBeUndefined();
  }
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  expect(onExecute).not.toHaveBeenCalled();
});
