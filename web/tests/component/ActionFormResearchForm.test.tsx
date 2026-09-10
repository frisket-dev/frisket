// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry, type ActionParamResolution } from '../../src/api/types';
import { GeneratedActionForm, type GeneratedActionFormProps } from '../../src/components/action-panel/GeneratedActionForm';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function renderResearch(question = 'Research {{country}}', diagnostics: ActionParamResolution['diagnostics'] = {}) {
  const entry = servedActionCatalog().actions.find((item) => item.kind === 'research.answer');
  if (!entry || !isGeneratedActionCatalogEntry(entry)) throw new Error('Missing typed research action');
  const template = generatedActionTemplateFromCatalogEntry(entry)!;
  const sheet = sheetMeta([
    columnDef({ id: '1', name: 'country', type: 'text' }),
    columnDef({ id: '2', name: 'product', type: 'text' }),
  ], { id: '7' });
  const resolveParams = vi.fn<GeneratedActionFormProps['resolveParams']>(async ({ params }) => ({
    diagnostics,
    logical_outputs: [
      { key: 'answer', column_type: 'text' },
      ...(params.include_sources ? [{ key: 'sources', column_type: 'json' }] : []),
    ],
  }));
  const onExecute = vi.fn();
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No provider requests in this test')));
  render(<GeneratedActionForm catalogEntry={entry} actionTemplate={template} sheet={sheet}
    initialDraft={{ action_id: 'research.answer', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: ['country'], question: { text: question }, model: 'test/model',
        include_sources: entry.input_schema.properties?.include_sources?.default === true },
      output_names: { answer: 'answer', sources: 'answer_sources' } }}
    running={false} resolveParams={resolveParams} onExecute={onExecute} onClose={vi.fn()} />);
  return { resolveParams, onExecute };
}

it('submits declared answer and sources outputs without a freeform field editor', async () => {
  const { onExecute } = renderResearch();
  expect(screen.queryByTestId('output-field-name')).not.toBeInTheDocument();
  expect(screen.queryByText('Add column')).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
    action_id: 'research.answer', output_names: { answer: 'answer', sources: 'answer_sources' },
  }), 'run');
});

it('defaults Include sources ON and resolves the answer-only output when switched OFF', async () => {
  const { resolveParams, onExecute } = renderResearch();
  const toggle = screen.getByTestId('field-include_sources');
  expect(toggle).toBeChecked();
  fireEvent.click(toggle);
  expect(toggle).not.toBeChecked();
  await waitFor(() => expect(resolveParams).toHaveBeenLastCalledWith(expect.objectContaining({
    params: expect.objectContaining({ include_sources: false }),
  })));
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
    params: expect.objectContaining({ include_sources: false }),
    output_names: { answer: 'answer' },
  }), 'run');
});

it('a column chip inserts a token into the typed research question', () => {
  renderResearch('');
  fireEvent.click(screen.getByRole('button', { name: 'country' }));
  expect(screen.getByTestId('field-question')).toHaveValue('{{country}}');
});

it('shows server placeholder diagnostics inline and gates Run', async () => {
  const { resolveParams } = renderResearch('Impact on {{regoin}} economy', {
    question: { ok: false, message: 'No column named "regoin" on this sheet.' },
  });
  await waitFor(() => expect(resolveParams).toHaveBeenCalled());
  expect(await screen.findByTestId('field-question-error')).toHaveTextContent('No column named "regoin"');
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
});

it('a valid template remains runnable and crosses the request boundary as typed text', async () => {
  const { onExecute } = renderResearch('Impact on {{country}} {{product}}');
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
    params: expect.objectContaining({ question: { text: 'Impact on {{country}} {{product}}' } }),
  }), 'run');
  expect(screen.queryByTestId('field-question-error')).not.toBeInTheDocument();
});
