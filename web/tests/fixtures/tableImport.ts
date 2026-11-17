import type { TableImportDraft } from '../../src/api/tableImports';

export function draft(patch: Partial<TableImportDraft> = {}): TableImportDraft {
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

