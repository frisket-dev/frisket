// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import type { GeneratedActionCatalogEntry, GeneratedActionDraft } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { sheetMeta } from '../support/actionFormFixtures';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { columnDef } from '../support/domainFixtures';

beforeAll(installPopoverPolyfill);
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const SHEET = sheetMeta([
  columnDef({ id: '1', name: 'raw', type: 'text' }),
  columnDef({ id: '2', name: 'notes', type: 'text' }),
], { id: '7', rowCount: 2 });
const SOURCE_ERROR = 'Choose at least one input column.';

function mount(options: {
  initialSourceColumn?: string;
  initialDraft?: GeneratedActionDraft;
  control?: 'rich_source' | 'column_or_template';
} = {}) {
  const single = options.control === 'column_or_template';
  const entry = syntheticActionCatalogEntry('map.source_picker_test', {
    input_schema: {
      type: 'object', required: ['source'], properties: {
        source: single ? { type: 'string', title: 'Read from' }
          : { type: 'array', items: { type: 'string' }, minItems: 1, title: 'Read from' },
      },
    },
    ui_hints: {
      form: 'generated', category: 'text',
      semantic_controls: { source: options.control ?? 'rich_source' },
      source_requirements: [{ id: 'source', param: 'source', mode: single ? 'column' : 'columns',
        label: 'Read from', min: 1, accepted_column_types: ['text'] }],
      logical_outputs: [{ key: 'value', column_type: 'text' }],
    },
  }) as GeneratedActionCatalogEntry;
  const template = generatedActionTemplateFromCatalogEntry(entry);
  if (!template) throw new Error('Missing source picker template');
  const onExecute = vi.fn();
  const resolveParams = vi.fn(async ({ params }: { params: Record<string, unknown> }) => ({
    diagnostics: Array.isArray(params.source) && params.source.length === 0
      ? { source: { ok: false, message: SOURCE_ERROR } } : {},
    logical_outputs: [{ key: 'value', column_type: 'text' }],
  }));
  render(<GeneratedActionForm catalogEntry={entry} actionTemplate={template}
    sheet={SHEET} running={false} initialSourceColumn={options.initialSourceColumn}
    initialDraft={options.initialDraft} resolveParams={resolveParams}
    onExecute={onExecute} onClose={vi.fn()} />);
  return { resolveParams, onExecute };
}

function saved(source: string[] | { text: string }): GeneratedActionDraft {
  return { action_id: 'map.source_picker_test', scope: { kind: 'sheet_rows', sheet_id: 7 },
    params: { source }, output_names: { value: 'value' } };
}

describe('new multi-column source choices', () => {
  it.each([undefined, 'raw'])('starts empty and quiet, including contextual column %s', async (initialSourceColumn) => {
    const { resolveParams, onExecute } = mount({ initialSourceColumn });
    expect(screen.getByTestId('text-source-mode-columns')).toHaveAttribute('aria-pressed', 'true');
    expect(within(screen.getByTestId('text-source-columns')).queryByRole('button', { name: /^Remove / }))
      .not.toBeInTheDocument();
    await waitFor(() => expect(resolveParams).toHaveBeenCalledWith(expect.objectContaining({ params: { source: [] } })));
    expect(screen.queryByText(SOURCE_ERROR)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('requires an explicit choice and shows validation after that choice is cleared', async () => {
    const { resolveParams, onExecute } = mount();
    fireEvent.mouseDown(screen.getByTestId('text-source-columns'));
    fireEvent.click(screen.getByRole('option', { name: /^notes/ }));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({ params: { source: ['notes'] } }), 'run');
    fireEvent.click(screen.getByRole('button', { name: 'Remove notes' }));
    await waitFor(() => expect(resolveParams).toHaveBeenLastCalledWith(expect.objectContaining({ params: { source: [] } })));
    expect(await screen.findByText(SOURCE_ERROR)).toBeVisible();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  });

  it('keeps unrelated server refusals visible for an untouched empty source', async () => {
    const { resolveParams } = mount();
    resolveParams.mockResolvedValue({ diagnostics: { __all__: { ok: false, message: 'This action is unavailable.' } }, logical_outputs: [] });
    expect(await screen.findByText('This action is unavailable.')).toBeVisible();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  });

  it('retains explicit saved and rerun column selections', async () => {
    const { resolveParams } = mount({ initialDraft: saved(['notes']) });
    expect(screen.getByRole('button', { name: 'Remove notes' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove raw' })).not.toBeInTheDocument();
    await waitFor(() => expect(resolveParams).toHaveBeenCalledWith(expect.objectContaining({ params: { source: ['notes'] } })));
    expect(screen.getByTestId('generated-action-run')).toBeEnabled();
  });

  it('shows an existing saved empty-source refusal immediately', async () => {
    mount({ initialDraft: saved([]) });
    expect(await screen.findByText(SOURCE_ERROR)).toBeVisible();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  });

  it('does not invent a selection when switching a saved template to columns', async () => {
    const { resolveParams } = mount({ initialDraft: saved({ text: '{{notes}}' }) });
    expect(screen.getByTestId('text-source-mode-template')).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByTestId('text-source-mode-columns'));
    await waitFor(() => expect(resolveParams).toHaveBeenLastCalledWith(expect.objectContaining({ params: { source: [] } })));
    expect(screen.queryByRole('button', { name: /^Remove / })).not.toBeInTheDocument();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  });

  it.each([undefined, 'notes'])('preserves the single-column default with context %s', async (initialSourceColumn) => {
    const { resolveParams } = mount({ control: 'column_or_template', initialSourceColumn });
    await waitFor(() => expect(resolveParams).toHaveBeenCalledWith(expect.objectContaining({ params: { source: initialSourceColumn ?? 'raw' } })));
    expect(screen.getByTestId('generated-action-run')).toBeEnabled();
  });
});
