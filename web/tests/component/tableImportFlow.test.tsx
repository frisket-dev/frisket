// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { TableImportDraft, TableImportMappingRequest, TableImportsApi } from '../../src/api/tableImports';
import { TableImportFlow } from '../../src/components/importWorkspace/tableImport';
import { ImportTableStep } from '../../src/components/importWorkspace/tableImport/TableImportSteps';
import { importSessionSnapshot } from '../../src/components/importProgressSession';

afterEach(() => { document.body.innerHTML = ''; });

function draft(patch: Partial<TableImportDraft> = {}): TableImportDraft {
  return {
    schema_version: 'frisket.table_import.v1',
    id: 'table-draft-1',
    draft_id: 'table-draft-1',
    state: 'ready',
    revision: 3,
    created_at: '2026-10-18T00:00:00Z',
    updated_at: '2026-10-18T00:00:00Z',
    source: { input_kind: 'file', filename: 'contributions.csv', size: 120, sha256: 'sha256:test', format: 'delimited', sheets: [], can_rerun_workbook: false },
    settings: {
      revision: 0,
      requested: { delimiter: null, encoding: null, decimal_separator: 'auto', thousands_separator: 'auto', worksheet: null, skip_rows_start: 0, first_row_is_header: true, skip_rows_end: 0 },
      resolved: { kind: 'delimited', delimiter: ',', encoding: 'utf-8', decimal_separator: '.', thousands_separator: ',', worksheet: null, skip_rows_start: 0, first_row_is_header: true, skip_rows_end: 0 },
    },
    analysis: { state: 'complete', job_id: 1, scanned_rows: 2, estimated_rows: 2, exact_rows: 2, complete: true, issue_counts: { extra_values: 0, missing_values: 0, malformed_records: 0, unreadable_cells: 0 }, error: null },
    preview: {
      columns: [{ index: 0, name: 'contributor_id', original_name: 'contributor_id', name_status: 'original' }],
      head: [
        { role: 'header', record_ordinal: 1, physical_line_start: 1, physical_line_end: 1, cells: ['contributor_id'], hidden_cell_count: 0, issues: [] },
        { role: 'data', record_ordinal: 2, physical_line_start: 2, physical_line_end: 2, cells: ['00418823'], hidden_cell_count: 0, issues: [] },
      ],
      tail: [], rows_seen: 2, omitted_rows: 0, complete: true, tail_pending: false, expected_values: 1,
      notice: { kind: 'consistent', count: null, common_values: 1, expected_values: 1, encoding: null },
    },
    columns: [{
      source_index: 0, name: 'contributor_id', original_name: 'contributor_id', name_status: 'original', included: true,
      target_type: 'text', date_order: null, suggested_type: 'text', ambiguous_date_order: false, needs_review: ['leading_zeros'],
      inferred_date_order: null, conversion_counts_by_date_order: null,
      samples: [{ original: '00418823', value: '00418823', display: '00418823', outcome: 'unchanged' }],
      conversion_counts: { rows_checked: 2, valid: 2, blank: 0, changed: 0, invalid: 0, leading_zero_loss: 0, time_dropped: 0 }, stats_complete: true,
    }],
    destination: null,
    can_commit: false,
    commit_blocked_reason: 'destination',
    commit: null,
    ...patch,
  };
}

function mockApi(initial = draft()) {
  const updateDestination = vi.fn(async (_project: string, _id: string, _revision: number, destination: { kind: string }, mappings?: TableImportMappingRequest[]) => draft({
    revision: 4,
    destination: {
      configuration: destination as never,
      mappings: [{ source_index: 0, source_name: 'contributor_id', source_type: 'text', target: (mappings?.[0]?.target ?? (destination.kind === 'existing' ? { kind: 'existing', column_id: 77 } : { kind: 'new', name: 'contributor_id' })) as never, match: destination.kind === 'existing' ? 'matched' : 'new', detail: null }],
      dedup: { enabled: false, key_source_index: null },
      comparison: { state: 'ready', matched_columns: 0, new_columns: 1, type_clashes: 0, duplicate_rows: 0, import_rows: 2, complete: true, error: null },
    },
    can_commit: true,
    commit_blocked_reason: null,
  }));
  const api: TableImportsApi = {
    createFromFile: vi.fn(async () => initial),
    createFromPaste: vi.fn(async () => initial),
    get: vi.fn(async () => initial),
    rerun: vi.fn(async () => initial),
    unlock: vi.fn(async () => initial),
    updateSettings: vi.fn(async () => initial),
    updateColumns: vi.fn(async () => initial),
    updateDestination,
    commit: vi.fn(async () => ({ schema_version: 'frisket.table_import.v1', draft_id: initial.draft_id, status: 'accepted', import_ref: 'table-import-1', sheet_id: 41, sheet_name: 'contributions', selected_total: 2 })),
    discard: vi.fn(async () => undefined),
  };
  return api;
}

function renderFlow(api: TableImportsApi, props: Partial<ComponentProps<typeof TableImportFlow>> = {}) {
  const onImported = vi.fn();
  render(<TableImportFlow
    api={api}
    projectId="project-1"
    initialFile={new File(['contributor_id\n00418823'], 'contributions.csv', { type: 'text/csv' })}
    destinationSheets={[]}
    onContextChange={vi.fn()}
    registerCloseGuard={vi.fn()}
    onImported={onImported}
    onDiscarded={vi.fn()}
    onError={vi.fn()}
    onUseMultipleFiles={vi.fn()}
    onUsePdfSplit={vi.fn()}
    onUseFollowTheMoney={vi.fn()}
    {...props}
  />);
  return { onImported };
}

describe('TableImportFlow', () => {
  it('uses the staged draft across all four steps and defaults to a new sheet', async () => {
    const api = mockApi();
    const progressVersion = importSessionSnapshot();
    const { onImported } = renderFlow(api);

    expect(await screen.findByLabelText('Check rows')).toBeVisible();
    expect(api.createFromFile).toHaveBeenCalledTimes(1);
    expect(screen.getByText('00418823')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Columns →' }));

    expect(await screen.findByLabelText('Columns')).toBeVisible();
    expect(screen.getByText('Needs a look 1')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Continue →' }));

    expect(await screen.findByLabelText('Import')).toBeVisible();
    expect(screen.getByRole('radio', { name: /New sheet/ })).toBeChecked();
    expect(screen.getByRole('textbox', { name: 'Sheet name' })).toHaveValue('contributions');
    await waitFor(() => expect(api.updateDestination).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole('button', { name: 'Import 2 rows' }));
    await waitFor(() => expect(onImported).toHaveBeenCalledWith(41));
    expect(importSessionSnapshot()).toBeGreaterThan(progressVersion);
  });

  it('does not commit a previously saved destination when the required final save fails', async () => {
    const api = mockApi();
    renderFlow(api);
    fireEvent.click(await screen.findByRole('button', { name: 'Columns →' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Continue →' }));
    expect(await screen.findByLabelText('Import')).toBeVisible();
    await waitFor(() => expect(api.updateDestination).toHaveBeenCalledTimes(1));

    vi.mocked(api.updateDestination).mockRejectedValueOnce(new Error('Could not save destination'));
    fireEvent.click(screen.getByRole('button', { name: 'Import 2 rows' }));

    expect(await screen.findByText('Could not save destination')).toBeVisible();
    expect(api.commit).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Import')).toBeVisible();
  });

  it('keeps several selected files intact when handing off to Multiple files', async () => {
    const api = mockApi();
    const onUseMultipleFiles = vi.fn();
    renderFlow(api, { initialFile: null, onUseMultipleFiles });
    const files = [new File(['a'], 'a.csv'), new File(['b'], 'b.xlsx')];
    fireEvent.change(screen.getByRole('button', { name: 'Choose file' }).closest('.table-import-dropzone')!.querySelector('input')!, { target: { files } });
    expect(await screen.findByRole('heading', { name: 'One file at a time' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Use Multiple files' }));
    await waitFor(() => expect(onUseMultipleFiles).toHaveBeenCalledWith(files));
    expect(api.createFromFile).not.toHaveBeenCalled();
  });

  it('hands the selected PDF itself to Split a PDF', async () => {
    const api = mockApi();
    const onUsePdfSplit = vi.fn();
    renderFlow(api, { initialFile: null, onUsePdfSplit });
    const file = new File(['%PDF'], 'packet.pdf', { type: 'application/pdf' });
    fireEvent.change(screen.getByRole('button', { name: 'Choose file' }).closest('.table-import-dropzone')!.querySelector('input')!, { target: { files: [file] } });
    fireEvent.click(await screen.findByRole('button', { name: 'Split a PDF' }));
    await waitFor(() => expect(onUsePdfSplit).toHaveBeenCalledWith(file));
  });

  it('sends an extensionless file to server content inspection as soon as it is chosen', async () => {
    const api = mockApi();
    renderFlow(api, { initialFile: null });
    const file = new File(['name\nAda\n'], 'uploaded-table', { type: 'application/octet-stream' });
    const input = screen.getByRole('button', { name: 'Choose file' }).closest('.table-import-dropzone')!.querySelector('input')!;

    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(api.createFromFile).toHaveBeenCalledWith('project-1', file, expect.anything()));
    expect(await screen.findByLabelText('Check rows')).toBeVisible();
  });

  it('uses record ordinals when a displayed physical line follows a multiline record', async () => {
    const initial = draft({
      preview: {
        ...draft().preview!,
        head: [{
          role: 'header', record_ordinal: 2, physical_line_start: 3, physical_line_end: 3,
          cells: ['contributor_id'], hidden_cell_count: 0, issues: [],
        }],
      },
    });
    const api = mockApi(initial);
    renderFlow(api);
    fireEvent.click(await screen.findByRole('button', { name: '3' }));

    await waitFor(() => expect(api.updateSettings).toHaveBeenCalledWith(
      'project-1', initial.draft_id, initial.revision,
      expect.objectContaining({ skip_rows_start: 1 }),
    ));
  });

  it('reopens a retained workbook by import ref and starts a new draft for its current sheet', async () => {
    const retained = draft({
      draft_id: 'import-ref-7', id: 'import-ref-7', state: 'committed',
      source: {
        ...draft().source, filename: 'workbook.xlsx', format: 'xlsx', can_rerun_workbook: true,
        sheets: [{ index: 0, name: 'January', hidden: false, empty: false }, { index: 1, name: 'February', hidden: false, empty: false }],
      },
      settings: {
        ...draft().settings!,
        requested: { ...draft().settings!.requested, worksheet: 'January' },
        resolved: { ...draft().settings!.resolved, kind: 'xlsx', delimiter: null, encoding: null, worksheet: 'January' },
      },
    });
    const next = draft({
      draft_id: 'rerun-draft-8', id: 'rerun-draft-8', state: 'ready',
      source: retained.source,
      settings: retained.settings,
    });
    const api = mockApi(next);
    vi.mocked(api.get).mockResolvedValue(retained);
    vi.mocked(api.rerun).mockResolvedValue(next);

    renderFlow(api, { initialFile: null, initialRerunDraftId: retained.draft_id });

    expect(await screen.findByLabelText('Check rows')).toBeVisible();
    expect(api.get).toHaveBeenCalledWith('project-1', 'import-ref-7', expect.anything());
    expect(api.rerun).toHaveBeenCalledWith('project-1', 'import-ref-7', 'January', expect.anything());
    expect(screen.getByRole('combobox', { name: /Sheet/ })).toHaveValue('January');
  });

  it('shows the inferred date order until the user chooses another order', async () => {
    const initial = draft({
      columns: [{
        ...draft().columns[0], target_type: 'date', suggested_type: 'date', ambiguous_date_order: true,
        date_order: null, inferred_date_order: 'day_first', needs_review: ['ambiguous_date_order'],
      }],
    });
    const api = mockApi(initial);
    renderFlow(api);
    fireEvent.click(await screen.findByRole('button', { name: 'Columns →' }));

    expect(await screen.findByRole('combobox', { name: 'Date order for contributor_id' })).toHaveValue('day_first');
  });

  it('lets an existing-sheet column match be changed to a new column', async () => {
    const api = mockApi();
    renderFlow(api, { destinationSheets: [{ id: 9, name: 'People', rowCount: 4, columns: [{ id: 77, name: 'contributor_id' }] }] });
    fireEvent.click(await screen.findByRole('button', { name: 'Columns →' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Continue →' }));
    fireEvent.click(await screen.findByRole('radio', { name: /Add to an existing sheet/ }));

    const mapping = await screen.findByRole('combobox', { name: 'Map contributor_id' });
    expect(mapping).toHaveValue('existing:77');
    fireEvent.change(mapping, { target: { value: 'new' } });
    await waitFor(() => expect(api.updateDestination).toHaveBeenLastCalledWith(
      'project-1', expect.any(String), expect.any(Number),
      expect.objectContaining({ kind: 'existing', sheet_id: 9 }),
      [expect.objectContaining({ source_index: 0, target: { kind: 'new', name: 'contributor_id' } })],
      expect.anything(),
    ));
  });

  it('selects the shown range between a checked column and a shift-clicked column', async () => {
    const baseColumn = draft().columns[0];
    const columns = ['first', 'second', 'third'].map((name, source_index) => ({ ...baseColumn, source_index, name, original_name: name }));
    const api = mockApi(draft({ columns }));
    renderFlow(api);
    fireEvent.click(await screen.findByRole('button', { name: 'Columns →' }));

    fireEvent.mouseDown(screen.getByRole('checkbox', { name: 'Select first' }));
    const third = screen.getByRole('checkbox', { name: 'Select third' });
    fireEvent.keyDown(third, { key: 'Shift' });
    fireEvent.mouseDown(third);
    fireEvent.keyUp(third, { key: 'Shift' });

    expect(screen.getByText('3 columns selected')).toBeVisible();
  });

  it('shows generated column names when the first row is data', async () => {
    const initial = draft({
      settings: { ...draft().settings!, requested: { ...draft().settings!.requested, first_row_is_header: false } },
      preview: {
        ...draft().preview!,
        columns: [
          { index: 0, name: 'column 1', original_name: null, name_status: 'missing' },
          { index: 1, name: 'column 2', original_name: null, name_status: 'missing' },
        ],
        head: [{ role: 'data', record_ordinal: 1, physical_line_start: 1, physical_line_end: 1, cells: ['Ada', '42'], hidden_cell_count: 0, issues: [] }],
      },
    });
    renderFlow(mockApi(initial));

    const names = await screen.findByRole('row', { name: 'Generated column names' });
    expect(names).toHaveTextContent('column 1');
    expect(names).toHaveTextContent('column 2');
    expect(within(names).queryByRole('button')).not.toBeInTheDocument();
  });

  it('offers dedup keys only for mappings into existing columns', () => {
    const base = draft();
    const columns = [
      { ...base.columns[0], source_index: 0, name: 'matched', original_name: 'matched' },
      { ...base.columns[0], source_index: 1, name: 'new_value', original_name: 'new_value' },
    ];
    const current = draft({
      columns,
      destination: {
        configuration: { kind: 'existing', sheet_id: 9 },
        mappings: [
          { source_index: 0, source_name: 'matched', source_type: 'text', target: { kind: 'existing', column_id: 77 }, match: 'matched', detail: null },
          { source_index: 1, source_name: 'new_value', source_type: 'text', target: { kind: 'new', name: 'new_value' }, match: 'new', detail: null },
        ],
        dedup: { enabled: false, key_source_index: null },
        comparison: { state: 'ready', matched_columns: 1, new_columns: 1, type_clashes: 0, duplicate_rows: null, import_rows: 2, complete: true, error: null },
      },
    });
    const onDestination = vi.fn();
    render(<ImportTableStep draft={current} destination={{ kind: 'existing', sheet_id: 9 }} destinationSheets={[{ id: 9, name: 'People', rowCount: 4, columns: [{ id: 77, name: 'matched' }] }]} busy={false} onBack={vi.fn()} onCommit={vi.fn()} onDestination={onDestination} />);

    const keySelect = screen.getByRole('combobox', { name: 'Duplicate key' });
    expect(within(keySelect).getByRole('option', { name: 'matched' })).toBeVisible();
    expect(within(keySelect).queryByRole('option', { name: 'new_value' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: /Skip rows already/ }));
    expect(onDestination).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({ dedup: { enabled: true, key_source_index: 0 } }));

    fireEvent.change(screen.getByRole('combobox', { name: 'Map matched' }), { target: { value: 'new' } });
    expect(onDestination).toHaveBeenLastCalledWith(expect.anything(), expect.objectContaining({ dedup: { enabled: false, key_source_index: null } }));
  });
});
