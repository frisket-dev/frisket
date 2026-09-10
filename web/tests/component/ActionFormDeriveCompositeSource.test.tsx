// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as api from '../../src/api/open';
import type { GeneratedActionDraft } from '../../src/api/types';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { DeriveActionForm } from '../../src/components/action-panel/DeriveActionForm';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';

const catalog = servedActionCatalog();
function entry(kind: string) {
  const value = catalog.actions.find((candidate) => candidate.kind === kind);
  if (!value || !isGeneratedActionCatalogEntry(value)) throw new Error('Missing typed ' + kind);
  return value;
}
const derive = entry('derive.table_from_list');
const extract = entry('map.extract');
const sheet = sheetMeta([
  columnDef({ id: '1', name: 'headline', type: 'text' }),
  columnDef({ id: '2', name: 'notes', type: 'text' }),
  columnDef({ id: '3', name: 'entities', type: 'json', ai: {} }),
], { id: '7' });

beforeEach(() => {
  vi.spyOn(api, 'listProviders').mockResolvedValue({ schemaVersion: 'frisket.providers.v1', tier: 'local',
    providers: [{ id: 'test', label: 'Test', kind: 'platform_api', configured: true, source: 'env', hint: null,
      models: [{ id: 'test/model', label: 'Test model', price: null }] }] });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function mount() {
  const onCompositeRun = vi.fn();
  const onExecute = vi.fn();
  render(<DeriveActionForm catalogEntry={derive} extractEntry={extract}
    actionTemplate={generatedActionTemplateFromCatalogEntry(derive)!} sheet={sheet}
    initialSourceColumn="entities" running={false} onClose={vi.fn()}
    resolveParams={async (request) => ({ diagnostics: {}, logical_outputs:
      request.action_id === 'map.extract' ? [{ key: 'items', column_type: 'json' }] : [] })}
    onExecute={onExecute} onCompositeRun={onCompositeRun} />);
  return { onCompositeRun, onExecute };
}

it('projects direct and template sources through the typed extraction boundary', async () => {
  const { onCompositeRun } = mount();
  fireEvent.click(screen.getByTestId('derive-source-mode-ai'));
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(onCompositeRun).toHaveBeenCalledWith(expect.objectContaining({
    intent: 'derive_from_extraction',
    extraction: expect.objectContaining({
      action_id: 'map.extract', params: expect.objectContaining({ source: ['entities'] }),
    }),
  }));
  fireEvent.click(screen.getByTestId('text-source-mode-template'));
  fireEvent.change(screen.getByTestId('text-source-template-input'), {
    target: { value: '{{headline}} — context' },
  });
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(onCompositeRun.mock.lastCall?.[0].extraction.params.source)
    .toEqual({ text: '{{headline}} — context' });
});

it('materializes an existing list column directly without an extraction request', async () => {
  const { onExecute, onCompositeRun } = mount();
  expect(screen.getByTestId('derive-source-mode-column')).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByTestId('derive-source-column-select')).toHaveValue('3');
  fireEvent.change(screen.getByTestId('field-sheet_name'), { target: { value: 'People' } });
  await waitFor(() => expect(screen.getByTestId('run-button')).toBeEnabled());
  fireEvent.click(screen.getByTestId('run-button'));
  expect(onCompositeRun).not.toHaveBeenCalled();
  expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
    action_id: 'derive.table_from_list', scope: { kind: 'project' }, sheet_name: 'People',
    params: expect.objectContaining({ source: { kind: 'column', sheet_id: 7, column_id: 3 } }),
  }), 'run');
});

it('keeps the list editor without a sheet, but does not invent an AI extraction source', async () => {
  const onExecute = vi.fn();
  const onCompositeRun = vi.fn();
  render(<DeriveActionForm catalogEntry={derive} extractEntry={extract}
    actionTemplate={generatedActionTemplateFromCatalogEntry(derive)!} sheet={null}
    running={false} onClose={vi.fn()}
    resolveParams={async () => ({ diagnostics: {}, logical_outputs: [], creates_sheet: true })}
    onExecute={onExecute} onCompositeRun={onCompositeRun} />);
  expect(screen.getByText(/Generate with AI needs a source sheet/)).toBeInTheDocument();
  expect(screen.getByTestId('list-table-params')).toBeInTheDocument();
  expect(screen.getByTestId('derive-source-column-select')).toBeDisabled();
  await waitFor(() => expect(screen.getByTestId('run-button')).toBeDisabled());
  expect(onExecute).not.toHaveBeenCalled();
  expect(onCompositeRun).not.toHaveBeenCalled();
});

it('preserves an explicit saved list source and custom options without a current sheet', async () => {
  const onExecute = vi.fn();
  const initialDraft: GeneratedActionDraft = { action_id: derive.kind, scope: { kind: 'project' },
    sheet_name: 'Saved people', output_names: {}, params: {
      source: { kind: 'named_result', sheet_id: 7, column_id: 4, run_id: 2, route: 'items', schema: 'item_list' },
      item_schema: { type: 'object', properties: { name: { type: 'string' } } },
    } };
  render(<DeriveActionForm catalogEntry={derive} extractEntry={extract}
    actionTemplate={generatedActionTemplateFromCatalogEntry(derive)!} sheet={null}
    initialDraft={initialDraft} running={false} onClose={vi.fn()}
    resolveParams={async () => ({ diagnostics: {}, logical_outputs: [], creates_sheet: true })}
    onExecute={onExecute} onCompositeRun={vi.fn()} />);
  expect(screen.getByTestId('derive-table-source-summary')).toHaveTextContent('Named result items from run 2 on sheet 7');
  await waitFor(() => expect(screen.getByTestId('generated-action-preview')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-preview'));
  expect(onExecute).toHaveBeenCalledWith({ ...initialDraft, idempotency_key: expect.any(String) }, 'preview');
});
