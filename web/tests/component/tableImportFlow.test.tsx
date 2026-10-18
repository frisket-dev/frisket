// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { TableImportDraft, TableImportsApi } from '../../src/api/tableImports';
import { TableImportFlow } from '../../src/components/importWorkspace/tableImport';

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
  const updateDestination = vi.fn(async (_project: string, _id: string, _revision: number, destination: { kind: string }) => draft({
    revision: 4,
    destination: {
      configuration: destination as never,
      mappings: [{ source_index: 0, source_name: 'contributor_id', source_type: 'text', target: { kind: 'new', name: 'contributor_id' }, match: 'new', detail: null }],
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
    const { onImported } = renderFlow(api);

    expect(screen.getByRole('button', { name: 'Check rows →' })).toBeEnabled();
    expect(api.createFromFile).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Check rows →' }));
    expect(await screen.findByLabelText('Check rows')).toBeVisible();
    expect(screen.getByText('00418823')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Columns →' }));

    expect(await screen.findByLabelText('Columns')).toBeVisible();
    expect(screen.getByText('Needs a look 1')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Continue →' }));

    expect(await screen.findByLabelText('Import')).toBeVisible();
    expect(screen.getByRole('radio', { name: /New sheet/ })).toBeChecked();
    expect(screen.getByRole('textbox', { name: 'Sheet name' })).toHaveValue('contributions');
    await waitFor(() => expect(api.updateDestination).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole('button', { name: 'Import 2 rows →' }));
    await waitFor(() => expect(onImported).toHaveBeenCalledWith(41));
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

  it('sends an extensionless file to server content inspection only after Check rows', async () => {
    const api = mockApi();
    renderFlow(api, { initialFile: null });
    const file = new File(['name\nAda\n'], 'uploaded-table', { type: 'application/octet-stream' });
    const input = screen.getByRole('button', { name: 'Choose file' }).closest('.table-import-dropzone')!.querySelector('input')!;

    fireEvent.change(input, { target: { files: [file] } });
    expect(api.createFromFile).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Check rows →' }));

    await waitFor(() => expect(api.createFromFile).toHaveBeenCalledWith('project-1', file, expect.anything()));
  });
});
