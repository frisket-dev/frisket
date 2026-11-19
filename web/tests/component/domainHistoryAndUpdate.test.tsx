// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { HistoryPanel } from '../../src/components/HistoryPanel';
import { KeyedUpdateControls } from '../../src/components/importWorkspace/tableImport/KeyedUpdateControls';
import { ImportTableStep } from '../../src/components/importWorkspace/tableImport/TableImportSteps';
import type { HistoryState } from '../../src/api/open';
import type { TableImportDestinationConfig, TableImportDestinationSnapshot } from '../../src/api/tableImports';
import { draft } from '../fixtures/tableImport';

afterEach(cleanup);
HTMLElement.prototype.scrollIntoView = vi.fn();

it('uses actual application state and never infers it from the navigation cursor', async () => {
  const revert = vi.fn(async () => {});
  const restore = vi.fn(async () => {});
  const history: HistoryState = {
    changes: [
      { id: '10', index: 0, label: 'Selectively reverted', kind: 'edit', at: '2026-11-15T00:00:00Z', rowsAffected: 1, status: 'undone', executable: true },
      { id: '11', index: 1, label: 'Later applied change', kind: 'edit', at: '2026-11-15T00:01:00Z', rowsAffected: 1, status: 'applied', executable: true },
    ],
    cursorIndex: 1, cursorChange: null, cursorChangeLoaded: true, undoTarget: { id: '11', index: 1, barrier: false }, redoTarget: null,
    offset: 0, limit: 50, total: 2, hasMoreBefore: false, hasMoreAfter: false, prevOffset: null, nextOffset: null,
    revision: { total: 2, maxChangeId: '11', changeCursor: '11' },
  };
  render(<HistoryPanel history={history} onRevert={revert} onRestore={restore} onLoadPage={async () => {}} focusChangeId={null} />);
  fireEvent.click(screen.getByTestId('history-revert-op-11'));
  await waitFor(() => expect(revert).toHaveBeenCalledWith('11'));
  fireEvent.click(screen.getByTestId('history-step-op-10').querySelector('button')!);
  expect(screen.getByTestId('history-step-op-10')).toHaveClass('history-state-undone');
  fireEvent.click(screen.getByTestId('history-restore-op-10'));
  await waitFor(() => expect(restore).toHaveBeenCalledWith('10'));
});

it('requires explicit update keys, preserves conflict policy, and clears append deduplication', () => {
  const initial = draft();
  initial.columns.push({ ...initial.columns[0], source_index: 1, name: 'amount', original_name: 'amount' });
  const onDestination = vi.fn();
  function Fixture() {
    const [destination, setDestination] = useState<TableImportDestinationConfig>({ kind: 'existing', sheet_id: 9 });
    const [snapshot, setSnapshot] = useState<TableImportDestinationSnapshot>({
      configuration: destination, mappings: initial.columns.map((column, index) => ({ source_index: index, source_name: column.name, source_type: 'text', target: { kind: 'existing', column_id: 70 + index }, match: 'matched', detail: null })),
      dedup: { enabled: true, key_source_index: 0 }, update_relationships: [],
      comparison: { state: 'ready', matched_columns: 2, new_columns: 0, type_clashes: 0, duplicate_rows: 0, import_rows: 2, complete: true, error: null },
    });
    return <ImportTableStep draft={{ ...initial, destination: snapshot, can_commit: true }} destination={destination}
      destinationSheets={[{ id: 9, name: 'People', rowCount: 5, columns: [{ id: 70, name: 'contributor_id' }, { id: 71, name: 'amount' }] }]}
      busy={false} onBack={() => {}} onCommit={() => {}} onDestination={(next, edited) => {
        onDestination(next, edited); setDestination(next); if (edited) setSnapshot(edited);
      }} />;
  }
  render(<Fixture />);
  fireEvent.click(screen.getByRole('button', { name: 'Update matching rows' }));
  expect(screen.getByRole('checkbox', { name: 'contributor_id' })).not.toBeChecked();
  expect(screen.getAllByRole('button', { name: 'Update matching rows' }).at(-1)).toBeDisabled();
  expect(screen.queryByRole('checkbox', { name: /Skip rows already/ })).toBeNull();
  fireEvent.click(screen.getByRole('checkbox', { name: 'contributor_id' }));
  expect(onDestination).toHaveBeenLastCalledWith(expect.objectContaining({ update: { key_source_indices: [0], policy: 'keep_changes' } }), expect.objectContaining({ dedup: { enabled: false, key_source_index: null } }));
  fireEvent.change(screen.getByLabelText('When imported values change'), { target: { value: 'incoming_wins' } });
  expect(onDestination).toHaveBeenLastCalledWith(expect.objectContaining({ update: { key_source_indices: [0], policy: 'incoming_wins' } }), expect.anything());
});


it('reuses a prior update source with its exact mapped keys and can start a new source', () => {
  const initial = draft();
  const onChange = vi.fn();
  const snapshot: TableImportDestinationSnapshot = {
    configuration: { kind: 'existing', sheet_id: 9 },
    mappings: [{ source_index: 0, source_name: 'contributor_id', source_type: 'text', target: { kind: 'existing', column_id: 70 }, match: 'matched', detail: null }],
    dedup: { enabled: false, key_source_index: null },
    update_relationships: [
      { relationship_id: 'prior', key_column_ids: [70], label: 'Earlier contributions.csv' },
      { relationship_id: 'unmapped', key_column_ids: [99], label: 'Unmapped keys' },
    ],
    comparison: { state: 'ready', matched_columns: 1, new_columns: 0, type_clashes: 0, duplicate_rows: 0, import_rows: 2, complete: true, error: null },
  };
  render(<KeyedUpdateControls destination={{ kind: 'existing', sheet_id: 9, update: { key_source_indices: [], policy: 'keep_changes' } }}
    snapshot={snapshot} columns={initial.columns} busy={false} onChange={onChange} />);
  expect(screen.getByRole('option', { name: 'Unmapped keys' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Update source'), { target: { value: 'prior' } });
  expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ update: { key_source_indices: [0], policy: 'keep_changes', relationship_id: 'prior' } }));
  fireEvent.change(screen.getByLabelText('Update source'), { target: { value: '' } });
  expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ update: { key_source_indices: [], policy: 'keep_changes', relationship_id: null } }));
});

it('selects the change a run link focused instead of the cursor change', () => {
  const history: HistoryState = {
    changes: [
      { id: '20', index: 0, label: 'Extract names', kind: 'map', at: '2026-11-15T00:00:00Z', rowsAffected: 1, status: 'applied', executable: true },
      { id: '21', index: 1, label: 'Later edit', kind: 'edit', at: '2026-11-15T00:01:00Z', rowsAffected: 1, status: 'applied', executable: true },
    ],
    cursorIndex: 1, cursorChange: null, cursorChangeLoaded: true, undoTarget: null, redoTarget: null,
    offset: 0, limit: 50, total: 2, hasMoreBefore: false, hasMoreAfter: false, prevOffset: null, nextOffset: null,
    revision: { total: 2, maxChangeId: '21', changeCursor: '21' },
  };
  const panel = (focusChangeId: string | null) => (
    <HistoryPanel history={history} onRevert={async () => {}} onRestore={async () => {}} onLoadPage={async () => {}} focusChangeId={focusChangeId} />
  );
  const { rerender } = render(panel(null));
  expect(screen.getByTestId('history-step-detail-op-21')).toBeInTheDocument();
  rerender(panel('20'));
  expect(screen.getByTestId('history-step-detail-op-20')).toBeInTheDocument();
});

