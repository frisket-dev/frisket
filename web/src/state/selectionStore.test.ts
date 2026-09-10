import { describe, expect, it } from 'vitest';
import { createSelectionStore, createSelectionState } from './selectionStore';
import { emptySelectedRows } from '../workspace/workspaceState';

describe('createSelectionStore', () => {
  it('starts with the same defaults createWorkspaceUiState used for the selection slice', () => {
    const { store } = createSelectionStore();
    expect(store.get()).toEqual({ selectedRows: emptySelectedRows(), selectedColumnId: null });
    expect(createSelectionState()).toEqual({ selectedRows: emptySelectedRows(), selectedColumnId: null });
  });

  it('setSelectedRows replaces the selection wholesale', () => {
    const { store, setSelectedRows } = createSelectionStore();
    setSelectedRows({ sheetId: 's1', rowIds: ['1', '2'], rowIndexes: [0, 1] });
    expect(store.get().selectedRows).toEqual({ sheetId: 's1', rowIds: ['1', '2'], rowIndexes: [0, 1] });
  });

  it('setSelectedColumnId sets/clears and bails (reference-equal) on a no-op set', () => {
    const { store, setSelectedColumnId } = createSelectionStore();
    const before = store.get();
    setSelectedColumnId(null); // already null
    expect(store.get()).toBe(before); // createStore's Object.is bail
    setSelectedColumnId('col-1');
    expect(store.get().selectedColumnId).toBe('col-1');
    expect(store.get()).not.toBe(before);
  });

  it('clearRowSelection resets to an empty selection scoped to the given sheet, leaving selectedColumnId untouched', () => {
    const { store, setSelectedRows, setSelectedColumnId, clearRowSelection } = createSelectionStore();
    setSelectedRows({ sheetId: 's1', rowIds: ['1'], rowIndexes: [0] });
    setSelectedColumnId('col-1');
    clearRowSelection('s1');
    expect(store.get().selectedRows).toEqual(emptySelectedRows('s1'));
    // applyGridFilter/applyGridSort/applySavedViewGrid/clearGridFilter/
    // clearGridSort/selectSheet never touched selectedColumnId — only
    // selectedRows.
    expect(store.get().selectedColumnId).toBe('col-1');
  });
});
