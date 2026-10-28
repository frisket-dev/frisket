// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';
import { lastExecutedRequest, mockLocalProviders, mountTypedForm, pressRun } from './cutoverUiF1TypedForm';

let dispose: (() => void) | undefined;
afterEach(() => { cleanup(); dispose?.(); dispose = undefined; vi.restoreAllMocks(); });
beforeEach(mockLocalProviders);
const sheet = () => sheetMeta([columnDef({ id: '1', name: 'company', type: 'text' })], { id: '7' });

it('runs Quick Search with the selected model and names both answer and search results', async () => {
  const mounted = mountTypedForm({ kind: 'research.web_search', sheet: sheet(), initialDraft: {
    action_id: 'research.web_search', scope: { kind: 'sheet_rows', sheet_id: 7 },
    params: { query: { text: '{{company}} headquarters' }, model: 'test/model', max_results: 5 },
  } });
  dispose = mounted.dispose;
  expect(screen.getByTestId('field-query')).toHaveValue('{{company}} headquarters');
  await pressRun();
  expect(lastExecutedRequest(mounted.onExecute)).toMatchObject({
    action_id: 'research.web_search',
    params: { query: { text: '{{company}} headquarters' }, model: 'test/model', max_results: 5 },
    output_names: { answer: 'answer', search_results: 'search_results' },
  });
});

it('uses the extraction field editor to author multiple structured research columns', async () => {
  const user = userEvent.setup();
  const mounted = mountTypedForm({ kind: 'research.extract', sheet: sheet(), initialDraft: {
    action_id: 'research.extract', scope: { kind: 'sheet_rows', sheet_id: 7 },
    params: { source: ['company'], model: 'test/model', question: { text: 'Research {{company}}.' },
      fields: [{ name: 'headquarters', type: 'text', description: 'Headquarters city' }],
      include_sources: true, include_confidence: false },
  }, resolveParams: async ({ params }) => ({ diagnostics: {}, logical_outputs: [
    ...(params.fields as Array<{ name: string; type: string }>).map(field => ({ key: field.name, column_type: field.type })),
    ...(params.include_sources ? [{ key: 'sources', column_type: 'json' }] : []),
  ] }) });
  dispose = mounted.dispose;
  expect(screen.getByTestId('field-question')).toHaveValue('Research {{company}}.');
  expect(screen.getByTestId('output-field-name')).toHaveValue('headquarters');
  await user.click(screen.getByRole('button', { name: 'Add column' }));
  fireEvent.change(screen.getAllByTestId('output-field-name')[1], { target: { value: 'founded' } });
  await user.click(screen.getAllByTestId('output-field-type')[1]);
  await user.click(screen.getByRole('option', { name: 'integer' }));
  await pressRun();
  expect(lastExecutedRequest(mounted.onExecute)).toMatchObject({
    action_id: 'research.extract',
    params: { source: ['company'], model: 'test/model', question: { text: 'Research {{company}}.' },
      fields: [{ name: 'headquarters', type: 'text' }, { name: 'founded', type: 'integer' }],
      include_sources: true, include_confidence: false },
    output_names: { headquarters: 'headquarters', founded: 'founded', sources: 'answer_sources' },
  });
  expect(screen.queryByTestId('mcp-tools-selector')).not.toBeInTheDocument();
});
