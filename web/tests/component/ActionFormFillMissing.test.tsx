// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { completeCatalogPayload, sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';

const catalog = completeCatalogPayload();
const entry = catalog.actions.find((candidate) => candidate.kind === 'resolve.fill_missing');
if (!entry || !isGeneratedActionCatalogEntry(entry)) {
  throw new Error('Missing generated resolve.fill_missing fixture');
}
const template = actionTemplatesFromCatalog(catalog)
  .find((candidate) => candidate.actionKind === entry.kind);
if (!template) throw new Error('Missing generated resolve.fill_missing template');

const SHEET = sheetMeta([
  columnDef({ id: '1', name: 'agency', type: 'text' }),
  columnDef({ id: '2', name: 'score', type: 'number' }),
  columnDef({ id: '3', name: 'attachment', type: 'file' }),
], { id: '7', rowCount: 4 });

afterEach(cleanup);

function mount(overrides: Partial<Parameters<typeof GeneratedActionForm>[0]> = {}) {
  const onExecute = vi.fn();
  const resolveParams = vi.fn(async () => ({
    diagnostics: {},
    logical_outputs: [{ key: 'cleaned', column_type: 'text' as const }],
  }));
  render(
    <GeneratedActionForm catalogEntry={entry} actionTemplate={template} sheet={SHEET}
      running={false} resolveParams={resolveParams} onExecute={onExecute}
      onClose={vi.fn()} {...overrides} />,
  );
  return { onExecute, resolveParams };
}

describe('generated resolve.fill_missing form', () => {
  it('authors canonical Params, follows the untouched output default, and stays whole-sheet', async () => {
    const { onExecute } = mount({ selectedRowIds: ['1', '2'] });

    expect(screen.getByTestId('field-source')).toHaveValue('agency');
    expect(screen.getByTestId('field-output-cleaned')).toHaveValue('agency_clean');
    expect(screen.queryByTestId('generated-action-run-scope-menu-button')).not.toBeInTheDocument();

    fireEvent.change(screen.getByTestId('field-source'), { target: { value: 'score' } });
    expect(screen.getByTestId('field-output-cleaned')).toHaveValue('score_clean');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));

    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({
      action_id: 'resolve.fill_missing',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'score', method: 'down', treat_blank_as_missing: true },
      output_names: { cleaned: 'score_clean' },
    }), 'run');
  });

  it('shows and submits fill_value only for the value method', async () => {
    const { onExecute } = mount();
    expect(screen.queryByTestId('field-fill_value')).not.toBeInTheDocument();

    fireEvent.change(screen.getByTestId('field-method'), { target: { value: 'value' } });
    expect(screen.getByTestId('field-fill_value')).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('field-fill_value'), { target: { value: 'Unknown' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));

    expect(onExecute.mock.calls[0][0].params).toEqual({
      source: 'agency', method: 'value', fill_value: 'Unknown', treat_blank_as_missing: true,
    });

    onExecute.mockClear();
    fireEvent.change(screen.getByTestId('field-method'), { target: { value: 'down' } });
    expect(screen.queryByTestId('field-fill_value')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0].params).toEqual({
      source: 'agency', method: 'down', treat_blank_as_missing: true,
    });
  });

  it('does not replace an output name after the user edits it', () => {
    mount();
    fireEvent.change(screen.getByTestId('field-output-cleaned'), {
      target: { value: 'my_filled' },
    });
    fireEvent.change(screen.getByTestId('field-source'), { target: { value: 'score' } });
    expect(screen.getByTestId('field-output-cleaned')).toHaveValue('my_filled');
  });

  it('keeps preview advisory and authors the run independently', async () => {
    const { onExecute } = mount();
    await waitFor(() => expect(screen.getByTestId('generated-action-preview')).toBeEnabled());

    fireEvent.click(screen.getByTestId('generated-action-preview'));
    expect(onExecute.mock.calls[0][0]).not.toHaveProperty('input_snapshot');
    fireEvent.click(screen.getByTestId('generated-action-run'));

    expect(onExecute.mock.calls[1][0]).not.toHaveProperty('input_snapshot');
    expect(onExecute.mock.calls[1][0]).toMatchObject({
      action_id: 'resolve.fill_missing',
      params: { source: 'agency', method: 'down', treat_blank_as_missing: true },
      output_names: { cleaned: 'agency_clean' },
    });
  });
});
