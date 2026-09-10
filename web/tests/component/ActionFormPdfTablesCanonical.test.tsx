// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import type { ComponentProps } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as apiModule from '../../src/api/open';
import type { GeneratedActionDraft } from '../../src/api/types';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { decodeSavedActionSpec, encodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';
import type { PdfTablesPickerProps } from '../../src/components/PdfTablesPicker';

vi.mock('../../src/components/PdfTablesPicker', () => ({
  PdfTablesPicker: ({ column, onMaterialize, onExportTables }: PdfTablesPickerProps) => <div>
    <button onClick={() => onMaterialize({ columnName: column.name,
      includeColumns: ['vendor', 'amount'], targetName: 'Reviewed tables' })}>Build reviewed sheet</button>
    <button onClick={() => onExportTables?.({ columnName: column.name,
      groupBy: 'table_index', excludeColumns: ['source_row_id', 'source_filename'] })}>Export reviewed tables</button>
  </div>,
}));

const CATALOG = servedActionCatalog();
const ENTRY = CATALOG.actions.find((entry) => entry.kind === 'media.extract_pdf_tables')!;
const TEMPLATES = actionTemplatesFromCatalog(CATALOG);
const SHEET = sheetMeta([
  columnDef({ id: '1', name: 'first_pdf', type: 'file' }),
  columnDef({ id: '2', name: 'second_pdf', type: 'file' }),
  columnDef({ id: '3', name: 'case_id', type: 'text' }),
], { id: '7', rowCount: 2 });
let stores: WorkspaceStores;
beforeEach(() => {
  vi.spyOn(apiModule, 'getRuntimeConfig').mockResolvedValue({
    cache_mode: 'replay', live_calls_possible: true, cache_mode_editable: false,
    email_from_address: null, email_from_name: null, recipe_fence_posture: 'enforced',
  });
  stores = createWorkspaceStores('pdf-canonical');
  stores.actionCatalog.store.set(() => ({ status: 'ready', error: null,
    catalog: CATALOG, resolvedTemplates: TEMPLATES, version: 1 }));
  vi.spyOn(stores.projectApi, 'resolveActionParams').mockResolvedValue({
    diagnostics: {}, logical_outputs: [{ key: 'pdf_tables', column_type: 'json' }],
  });
});
afterEach(() => { cleanup(); stores.dispose(); vi.restoreAllMocks(); });
function mount(props: Partial<ComponentProps<typeof ActionPanel>> = {}) {
  return render(<WorkspaceStoresContext.Provider value={stores}>
    <ActionPanel sheet={SHEET} running={false} onRun={vi.fn()}
      onExecuteRegisteredAction={vi.fn()} routeActionKind="media.extract_pdf_tables" {...props} />
  </WorkspaceStoresContext.Provider>);
}
async function run() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
}
function draft(): GeneratedActionDraft {
  return { action_id: 'media.extract_pdf_tables', scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] },
    params: { source: 'second_pdf', mode: 'extract_table', table_mode: 'stream',
      extract_table: { pages: '2-4', table_index: 'all', options: { row_tol: 4 } } },
    output_names: { pdf_tables: 'saved_tables' } };
}

describe('typed PDF-table action consumers', () => {
  it('discovers the canonical action with one source and host-owned output control', async () => {
    expect(ENTRY.ui_hints.form).toBe('generated');
    mount();
    expect(await screen.findByTestId('field-source')).toHaveValue('first_pdf');
    expect(screen.getAllByTestId('field-source')).toHaveLength(1);
    expect(screen.getAllByTestId('field-output-pdf_tables')).toHaveLength(1);
    expect(screen.getByTestId('field-table_mode')).toHaveValue('auto');
    expect(screen.queryByTestId('field-output_name')).not.toBeInTheDocument();
  });

  it('runs edited source, options, output name and selected rows without legacy translation', async () => {
    const onExecute = vi.fn();
    mount({ selectedRowIds: ['101'], onExecuteRegisteredAction: onExecute });
    fireEvent.change(await screen.findByTestId('field-source'), { target: { value: 'second_pdf' } });
    fireEvent.change(screen.getByTestId('field-table_mode'), { target: { value: 'lattice' } });
    fireEvent.change(screen.getByTestId('field-output-pdf_tables'), { target: { value: 'bid_tables' } });
    fireEvent.click(screen.getByText('Pages and extraction options'));
    fireEvent.change(screen.getByTestId('field-extract_table'), {
      target: { value: '{"pages":"2-4","table_index":"all","options":{"line_scale":30}}' },
    });
    await run();
    expect(onExecute.mock.calls[0][0]).toEqual({ action_id: 'media.extract_pdf_tables',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] },
      params: { source: 'second_pdf', mode: 'extract_table', table_mode: 'lattice',
        extract_table: { pages: '2-4', table_index: 'all', options: { line_scale: 30 } } },
      output_names: { pdf_tables: 'bid_tables' }, idempotency_key: expect.any(String) });
    expect(stores.projectApi.resolveActionParams).toHaveBeenLastCalledWith(expect.objectContaining({
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] },
    }));
  });

  it('reopens saved options and scope for preview and run without retaining execution authority', async () => {
    const saved = draft();
    expect(encodeSavedActionSpec(decodeSavedActionSpec(CATALOG, saved))).toEqual(saved);
    const onExecute = vi.fn();
    mount({ inspectProposal: { seq: 1, title: 'Saved PDF extraction', spec: saved },
      routeActionKind: undefined, onExecuteRegisteredAction: onExecute });
    expect(await screen.findByTestId('field-source')).toHaveValue('second_pdf');
    expect(screen.getByTestId('field-output-pdf_tables')).toHaveValue('saved_tables');
    await waitFor(() => expect(screen.getByTestId('generated-action-preview')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-preview'));
    await run();
    expect(onExecute.mock.calls.map((call) => call[1])).toEqual(['preview', 'run']);
    for (const [request] of onExecute.mock.calls) {
      expect(request).toMatchObject(saved);
      expect(request).not.toHaveProperty('confirmation');
      expect(request).not.toHaveProperty('replace_existing');
    }
    expect(onExecute.mock.calls[0][0].idempotency_key).not.toBe(onExecute.mock.calls[1][0].idempotency_key);
  });

  it('keeps unfinished options editable while host diagnostics block execution', async () => {
    vi.mocked(stores.projectApi.resolveActionParams).mockResolvedValue({
      diagnostics: { extract_table: { ok: false, message: 'Choose valid extraction options.' } }, logical_outputs: [],
    });
    mount();
    fireEvent.click(await screen.findByText('Pages and extraction options'));
    fireEvent.change(screen.getByTestId('field-extract_table'), { target: { value: '{"pages":' } });
    expect(screen.getByTestId('field-extract_table')).toHaveValue('{"pages":');
    expect(await screen.findByTestId('field-extract_table-error')).toHaveTextContent('Choose valid extraction options.');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(screen.getByTestId('generated-action-preview')).toBeDisabled();
  });

  it('does not offer non-file columns as PDF sources', async () => {
    mount({ sheet: { ...SHEET, columns: [SHEET.columns[2]] } });
    expect(await screen.findByTestId('field-source')).toHaveValue('');
    expect(screen.getByTestId('field-source')).not.toHaveTextContent('case_id');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  });

  it('keeps the reviewed column and projection in typed materialize/export requests', async () => {
    vi.spyOn(stores.projectApi, 'getColumnRuns').mockResolvedValue({ currentRun: {
      actionKind: 'media.extract_pdf_tables' }, latestRun: null } as never);
    const onRun = vi.fn();
    mount({ onRun, sheet: { ...SHEET, columns: [...SHEET.columns,
      columnDef({ id: '12', name: 'saved_tables', type: 'json' })] },
    inspectProposal: { seq: 2, title: 'Review extraction', spec: draft() }, routeActionKind: undefined });
    fireEvent.click(await screen.findByTestId('review-tables'));
    fireEvent.click(screen.getByText('Build reviewed sheet'));
    expect(onRun.mock.calls[0][0]).toMatchObject({ action_id: 'derive.table_from_list',
      scope: { kind: 'project' }, sheet_name: 'Reviewed tables',
      params: { source: { kind: 'column', sheet_id: 7, column_id: 12,
        include_columns: ['vendor', 'amount'] } } });
    fireEvent.click(screen.getByTestId('review-tables'));
    fireEvent.click(screen.getByText('Export reviewed tables'));
    expect(onRun.mock.calls[1][0]).toMatchObject({ action_id: 'export.column_tables',
      params: { sheet_id: 7, column_id: 12, group_by: 'table_index',
        exclude_columns: ['source_row_id', 'source_filename'] } });
  });
});
