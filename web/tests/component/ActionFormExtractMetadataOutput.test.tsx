// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { GeneratedActionCatalogEntry, GeneratedActionDraft } from '../../src/api/types';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { aiMeta, columnDef } from '../support/domainFixtures';
import { completeCatalogPayload, sheetMeta } from '../support/actionFormFixtures';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';

// Representative server fields, including an unfamiliar role: the UI must not
// maintain a second registry of the metadata producer's output schema.
const COLUMN_OUTPUTS = [
  { key: 'details', column_type: 'json' },
  { key: 'size_bytes', column_type: 'integer' },
  { key: 'future_role', column_type: 'text' },
];
const ENTRY = syntheticActionCatalogEntry('media.extract_metadata', {
  title: 'Extract media metadata',
  input_schema: {
    type: 'object', additionalProperties: false, required: ['source'],
    properties: {
      source: { type: 'string', title: 'Source' },
      output_mode: { type: 'string', enum: ['object', 'columns'], default: 'columns' },
      refresh: { type: 'boolean', default: false },
    },
  },
  ui_hints: {
    form: 'generated', category: 'extract', semantic_controls: { source: 'column' },
    logical_outputs: COLUMN_OUTPUTS, dynamic_outputs: true,
    source_requirements: [{
      id: 'source', mode: 'column', param: 'source', label: 'Media source', min: 1,
      accepted_column_types: ['image', 'audio', 'video', 'file'],
    }],
  },
}) as GeneratedActionCatalogEntry;
const SHEET = sheetMeta([
  columnDef({ id: '1', name: 'asset', type: 'file' }),
  columnDef({ id: '2', name: 'caption', type: 'text' }),
], { id: '7', name: 'Media', rowCount: 2 });

function renderMetadata(options: {
  initialDraft?: GeneratedActionDraft;
  sheet?: typeof SHEET;
} = {}) {
  const template = actionTemplatesFromCatalog(completeCatalogPayload([ENTRY]))
    .find((candidate) => candidate.actionKind === ENTRY.kind);
  if (!template) throw new Error('Missing canonical metadata template');
  const onExecute = vi.fn();
  const resolveParams = vi.fn(async ({ params }: { params: Record<string, unknown> }) => ({
    diagnostics: {},
    logical_outputs: params.output_mode === 'columns' ? COLUMN_OUTPUTS : COLUMN_OUTPUTS.slice(0, 1),
  }));
  render(<GeneratedActionForm catalogEntry={ENTRY} actionTemplate={template}
    sheet={options.sheet ?? SHEET} selectedRowIds={['9']} running={false}
    initialDraft={options.initialDraft} resolveParams={resolveParams}
    onExecute={onExecute} onClose={vi.fn()} />);
  return { onExecute, resolveParams, template };
}

beforeEach(() => {
  let sequence = 0;
  vi.stubGlobal('crypto', { randomUUID: () => `metadata-request-${++sequence}` });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('typed metadata output naming', () => {
  it.each(['selected', 'all'] as const)('uses the canonical generated launcher with trimmed names, refresh and %s row scope', async (rowScope) => {
    const { onExecute, resolveParams, template } = renderMetadata();
    expect(template.kind).toBe('media.extract_metadata');
    expect(template.generatedAction).toBe(true);
    const outputMode = screen.getByTestId('field-output_mode') as HTMLSelectElement;
    expect(outputMode).toHaveValue('columns');
    expect(Array.from(outputMode.options).map(({ value, label }) => ({ value, label }))).toEqual([
      { value: 'object', label: 'One column' },
      { value: 'columns', label: 'Multiple columns' },
    ]);
    expect(await screen.findByTestId('field-output-future_role')).toHaveValue('meta_future_role');
    expect(screen.getByTestId('field-output-details')).toHaveValue('meta_details');
    if (rowScope === 'all') {
      fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
      fireEvent.click(screen.getByTestId('generated-action-row-scope-all'));
      expect(onExecute).toHaveBeenLastCalledWith(expect.objectContaining({
        scope: { kind: 'sheet_rows', sheet_id: 7 },
      }), 'run');
    }
    fireEvent.change(screen.getByTestId('field-output-prefix'), { target: { value: '  AssetMeta_2  ' } });
    fireEvent.click(screen.getByTestId('field-refresh'));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    const scope = { kind: 'sheet_rows', sheet_id: 7,
      ...(rowScope === 'selected' ? { row_ids: [9] } : {}) };
    // Parameter admission must resolve the same scope that execution receives.
    expect(resolveParams).toHaveBeenLastCalledWith({
      action_id: 'media.extract_metadata', scope,
      params: { source: 'asset', output_mode: 'columns', refresh: true },
    });
    expect(onExecute).toHaveBeenLastCalledWith({
      action_id: 'media.extract_metadata',
      scope,
      params: { source: 'asset', output_mode: 'columns', refresh: true },
      output_names: {
        details: 'AssetMeta_2_details', size_bytes: 'AssetMeta_2_size_bytes',
        future_role: 'AssetMeta_2_future_role',
      },
      idempotency_key: expect.any(String),
    }, 'run');
  });

  it('starts with server-resolved keys and renames details when switching modes', async () => {
    const { onExecute } = renderMetadata();
    expect(await screen.findByTestId('field-output-future_role')).toHaveValue('meta_future_role');
    expect(screen.getByTestId('field-output-details')).toHaveValue('meta_details');
    fireEvent.change(screen.getByTestId('field-output-prefix'), { target: { value: 'AssetMeta' } });
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0].output_names).toEqual({
      details: 'AssetMeta_details', size_bytes: 'AssetMeta_size_bytes', future_role: 'AssetMeta_future_role',
    });
    fireEvent.change(screen.getByTestId('field-output_mode'), { target: { value: 'object' } });
    await waitFor(() => expect(screen.queryByTestId('field-output-future_role')).not.toBeInTheDocument());
    expect(screen.getByTestId('field-output-details')).toHaveValue('AssetMeta');
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[1][0].output_names).toEqual({ details: 'AssetMeta' });
    expect(onExecute.mock.calls[1][0].idempotency_key)
      .not.toBe(onExecute.mock.calls[0][0].idempotency_key);
  });

  it('does not label a prefix-selected deduped-looking name as automatic', async () => {
    renderMetadata({ sheet: sheetMeta([
      ...SHEET.columns,
      columnDef({ id: '3', name: 'details', type: 'json' }),
    ], { id: '7', name: 'Media', rowCount: 2 }) });
    await screen.findByTestId('field-output-details');
    fireEvent.change(screen.getByTestId('field-output-prefix'), { target: { value: 'details_2' } });
    expect(screen.getByTestId('field-output-details')).toHaveValue('details_2');
    expect(screen.queryByTestId('default-output-name-collision-details')).not.toBeInTheDocument();
  });

  it('previews the same canonical semantics with a fresh request key', async () => {
    const { onExecute } = renderMetadata();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-preview'));
    fireEvent.click(screen.getByTestId('generated-action-run'));
    const preview = onExecute.mock.calls[0][0];
    expect(onExecute.mock.calls[0][1]).toBe('preview');
    expect(onExecute.mock.calls[1][1]).toBe('run');
    expect(onExecute.mock.calls[1][0]).toEqual({
      ...preview, idempotency_key: expect.any(String),
    });
    expect(onExecute.mock.calls[1][0].idempotency_key).not.toBe(preview.idempotency_key);
    expect(preview).not.toHaveProperty('kind');
    expect(preview.params).toEqual({ source: 'asset', output_mode: 'columns', refresh: false });
    expect(preview.output_names).toEqual({
      details: 'meta_details', size_bytes: 'meta_size_bytes', future_role: 'meta_future_role',
    });
  });

  it('preserves saved individual names across non-shape parameter edits without consent', async () => {
    const names = { details: 'Custom_details', size_bytes: 'Bytes', future_role: 'Another name' };
    const { onExecute } = renderMetadata({ initialDraft: {
      action_id: 'media.extract_metadata', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'asset', output_mode: 'columns', refresh: false }, output_names: names,
    } });
    expect(await screen.findByTestId('field-output-size_bytes')).toHaveValue('Bytes');
    expect(screen.getByTestId('field-output-prefix')).toHaveValue('Custom');
    fireEvent.click(screen.getByTestId('field-refresh'));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0].output_names).toEqual(names);
    expect(onExecute.mock.calls[0][0]).not.toHaveProperty('replace_existing');
  });

  it('preserves an explicit saved one-column configuration', async () => {
    const { onExecute } = renderMetadata({ initialDraft: {
      action_id: 'media.extract_metadata', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'asset', output_mode: 'object', refresh: false },
      output_names: { details: 'Saved_metadata' },
    } });
    expect(screen.getByTestId('field-output_mode')).toHaveValue('object');
    expect(await screen.findByTestId('field-output-details')).toHaveValue('Saved_metadata');
    expect(screen.queryByTestId('field-output-future_role')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('field-refresh'));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toMatchObject({
      params: { source: 'asset', output_mode: 'object', refresh: true },
      output_names: { details: 'Saved_metadata' },
    });
  });

  it('refuses stale saved output keys instead of silently replacing saved intent', async () => {
    const { onExecute } = renderMetadata({ initialDraft: {
      action_id: 'media.extract_metadata', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'asset', output_mode: 'object', refresh: false },
      output_names: { old_details: 'Saved' },
    } });
    expect(await screen.findByText('Saved action outputs no longer match its parameters.')).toBeVisible();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.submit(screen.getByTestId('generated-action-form'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('uses generic blank and duplicate-name validation and allows individual adjustments', async () => {
    renderMetadata();
    await screen.findByTestId('field-output-details');
    fireEvent.change(screen.getByTestId('field-output-prefix'), { target: { value: '  ' } });
    expect(screen.getByText('Name every output column.')).toBeVisible();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.change(screen.getByTestId('field-output-prefix'), { target: { value: 'Meta' } });
    await screen.findByTestId('field-output-size_bytes');
    fireEvent.change(screen.getByTestId('field-output-size_bytes'), { target: { value: 'meta_DETAILS' } });
    expect(screen.getByText('Output column names must be unique.')).toBeVisible();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.change(screen.getByTestId('field-output-size_bytes'), { target: { value: 'Bytes' } });
    expect(screen.getByTestId('generated-action-run')).toBeEnabled();
  });

  it('offers compatible generated outputs and leaves replacement approval to the host', async () => {
    const { onExecute } = renderMetadata({ sheet: sheetMeta([
      ...SHEET.columns,
      columnDef({ id: '3', name: 'previous', type: 'json', ai: aiMeta(), generationManaged: true }),
      columnDef({ id: '4', name: 'wrong_type', type: 'text', ai: aiMeta(), generationManaged: true }),
      columnDef({ id: '5', name: 'manual_json', type: 'json' }),
    ], { id: '7', name: 'Media', rowCount: 2 }) });
    fireEvent.change(screen.getByTestId('field-output_mode'), { target: { value: 'object' } });
    const output = await screen.findByTestId('field-output-details');
    fireEvent.focus(output);
    expect(screen.getByTestId('field-output-details-option-previous')).toBeVisible();
    expect(screen.queryByTestId('field-output-details-option-wrong-type')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-output-details-option-manual-json')).not.toBeInTheDocument();
    fireEvent.change(output, { target: { value: 'previous' } });
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0].output_names).toEqual({ details: 'previous' });
    expect(onExecute.mock.calls[0][0]).not.toHaveProperty('replace_existing');
  });

  it.each(['image', 'audio', 'video', 'file'] as const)('accepts %s sources but not text', async (type) => {
    renderMetadata({ sheet: sheetMeta([
      columnDef({ id: '1', name: 'caption', type: 'text' }),
      columnDef({ id: '2', name: 'asset', type }),
    ], { id: '7', name: 'Media', rowCount: 2 }) });
    expect(screen.getByTestId('field-source')).toHaveValue('asset');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  });

  it('does not run when no compatible media source exists', async () => {
    const { onExecute } = renderMetadata({ sheet: sheetMeta([
      columnDef({ id: '1', name: 'caption', type: 'text' }),
    ], { id: '7', name: 'Media', rowCount: 2 }) });
    await screen.findByTestId('field-output-details');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(screen.getByTestId('generated-action-preview')).toBeDisabled();
    fireEvent.submit(screen.getByTestId('generated-action-form'));
    expect(onExecute).not.toHaveBeenCalled();
  });
});
