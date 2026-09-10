import { describe, expect, it } from 'vitest';
import { createDetailStore, createDetailState } from './detailStore';
import type { ColumnDef, PreviewCellDetail, Row } from '../api/open';

const row = (id: string): Row => ({ id, index: 0 }) as unknown as Row;
const column = (id: string): ColumnDef => ({ id, name: id, type: 'text' }) as unknown as ColumnDef;
const preview: PreviewCellDetail = {
  column: { name: 'sample', columnType: 'text', format: null, hidden: false, overwritesColumnId: null },
  cell: { value: null, error: 'preview failed' },
};

describe('createDetailStore', () => {
  it.each(['closeAll', 'clearRowDrawer', 'closeDrawers', 'clearForGridTransition', 'clearForSavedView'] as const)(
    '%s drops the sampled detail with its row', (method) => {
      const detail = createDetailStore();
      detail.openRow(row('r1'), preview);
      detail[method]();
      expect(detail.store.get().rowDrawer).toBeNull();
      expect(detail.store.get().rowDrawerPreview).toBeNull();
    },
  );

  it('switching to an unsampled row clears the sampled detail', () => {
    const detail = createDetailStore();
    detail.openRow(row('r1'), { ...preview, cell: { value: null } });
    expect(detail.store.get().rowDrawerPreview?.cell.value).toBeNull();
    detail.openRow(row('r2'));
    expect(detail.store.get().rowDrawerPreview).toBeNull();
  });

  it('starts with the same defaults createWorkspaceUiState used for the detail slice', () => {
    const { store } = createDetailStore();
    expect(store.get()).toEqual(createDetailState());
    expect(store.get().columnSettingsId).toBe('');
  });

  it('openRow (= former openRowPanel): opens the row drawer, closes column drawer/settings', () => {
    const { store, openColumn, openRow } = createDetailStore();
    openColumn(column('c1'));
    openRow(row('r1'), preview);
    const s = store.get();
    expect(s.rowDrawer).toEqual(row('r1'));
    expect(s.rowDrawerPreview).toBe(preview);
    expect(s.columnDrawer).toBeNull();
    expect(s.columnSettingsId).toBe('');
  });

  it('openRow with no preview defaults rowDrawerPreview to null', () => {
    const { store, openRow } = createDetailStore();
    openRow(row('r1'));
    expect(store.get().rowDrawerPreview).toBeNull();
  });

  it('openColumn (= former openColumnPanel): opens the column drawer, closes row drawer', () => {
    const { store, openRow, openColumn } = createDetailStore();
    openRow(row('r1'));
    openColumn(column('c1'));
    const s = store.get();
    expect(s.columnDrawer).toEqual(column('c1'));
    expect(s.columnSettingsId).toBe('c1');
    expect(s.rowDrawer).toBeNull();
  });

  it('closeAll (= former syncRouteClosed detail half): clears row/column drawer state', () => {
    const { store, openRow, openColumn, closeAll } = createDetailStore();
    openRow(row('r1'), preview);
    closeAll();
    let s = store.get();
    expect(s.rowDrawer).toBeNull();
    expect(s.rowDrawerPreview).toBeNull();
    expect(s.columnDrawer).toBeNull();
    expect(s.columnSettingsId).toBe('');

    openColumn(column('c1'));
    closeAll();
    s = store.get();
    expect(s.columnDrawer).toBeNull();
    expect(s.columnSettingsId).toBe('');
  });

  it('syncColumn (= former syncRouteColumn detail half): opens/closes the column drawer from a route projection', () => {
    const { store, openRow, syncColumn } = createDetailStore();
    openRow(row('r1'));
    syncColumn(column('c1'));
    let s = store.get();
    expect(s.rowDrawer).toBeNull();
    expect(s.rowDrawerPreview).toBeNull();
    expect(s.columnDrawer).toEqual(column('c1'));
    expect(s.columnSettingsId).toBe('c1');

    syncColumn(null);
    s = store.get();
    expect(s.columnDrawer).toBeNull();
    expect(s.columnSettingsId).toBe('');
  });

  it('syncRow (= former syncRouteRow detail half): closes the column drawer only', () => {
    const { store, openColumn, syncRow } = createDetailStore();
    openColumn(column('c1'));
    syncRow();
    const s = store.get();
    expect(s.columnDrawer).toBeNull();
    expect(s.columnSettingsId).toBe('');
  });

  it('loadRow (= former loadRouteRow): sets rowDrawer, clears rowDrawerPreview', () => {
    const { store, loadRow } = createDetailStore();
    loadRow(row('r1'));
    expect(store.get().rowDrawer).toEqual(row('r1'));
    expect(store.get().rowDrawerPreview).toBeNull();
    loadRow(null);
    expect(store.get().rowDrawer).toBeNull();
  });

  it('closeDrawers (= former clearDrawers detail half): options gate childFilter/columnSettingsId clears independently', () => {
    const { store, openRow, openColumn, setChildFilter, closeDrawers } = createDetailStore();
    openColumn(column('c1'));
    setChildFilter({ sheetId: 's1', parentSheetName: 'p', parentRowId: '1', parentRowIndex: 0, count: 1 });
    closeDrawers(); // no options: rowDrawer/columnDrawer clear, nothing else
    let s = store.get();
    expect(s.rowDrawer).toBeNull();
    expect(s.columnDrawer).toBeNull();
    expect(s.columnSettingsId).toBe('c1'); // NOT cleared — clearColumnSettings omitted
    expect(s.childFilter).not.toBeNull(); // NOT cleared — clearChildFilter omitted

    closeDrawers({ clearChildFilter: true, clearColumnSettings: true });
    s = store.get();
    expect(s.columnSettingsId).toBe('');
    expect(s.childFilter).toBeNull();

    openRow(row('r1'));
    closeDrawers({ clearChildFilter: true });
    expect(store.get().rowDrawer).toBeNull();
  });

  it('setChildFilter sets/clears independently of the drawers', () => {
    const { store, setChildFilter } = createDetailStore();
    const cf = { sheetId: 's1', parentSheetName: 'p', parentRowId: '1', parentRowIndex: 0, count: 3 };
    setChildFilter(cf);
    expect(store.get().childFilter).toEqual(cf);
    setChildFilter(null);
    expect(store.get().childFilter).toBeNull();
  });

  it('mergeColumnUpdate (= former columnUpdated): merges into an OPEN matching columnDrawer, preserving ai', () => {
    const { store, openColumn, mergeColumnUpdate } = createDetailStore();
    openColumn({ id: 'c1', name: 'c1', type: 'text', ai: true } as unknown as ColumnDef);
    mergeColumnUpdate({ id: 'c1', name: 'renamed', type: 'number' } as unknown as ColumnDef);
    const drawer = store.get().columnDrawer as unknown as { id: string; name: string; type: string; ai: boolean };
    expect(drawer.name).toBe('renamed');
    expect(drawer.type).toBe('number');
    expect(drawer.ai).toBe(true); // preserved from the OLD columnDrawer, not the incoming patch
  });

  it('mergeColumnUpdate opens a fresh columnDrawer when none is open', () => {
    const { store, mergeColumnUpdate } = createDetailStore();
    mergeColumnUpdate(column('c2'));
    expect(store.get().columnDrawer).toEqual(column('c2'));
  });

  it('mergeColumnUpdate is a no-op when the open columnDrawer is a DIFFERENT column', () => {
    const { store, openColumn, mergeColumnUpdate } = createDetailStore();
    openColumn(column('c1'));
    const before = store.get();
    mergeColumnUpdate(column('c2'));
    expect(store.get()).toBe(before); // createStore's Object.is bail
  });

  it('clearForGridTransition (= former applyGridFilter/applyGridSort detail half): clears childFilter + rowDrawer, NOT columnDrawer', () => {
    const { store, openRow, openColumn, setChildFilter, clearForGridTransition } = createDetailStore();
    openRow(row('r1'));
    openColumn(column('c1'));
    setChildFilter({ sheetId: 's1', parentSheetName: 'p', parentRowId: '1', parentRowIndex: 0, count: 1 });
    clearForGridTransition();
    const s = store.get();
    expect(s.childFilter).toBeNull();
    expect(s.rowDrawer).toBeNull();
    // Pins an intentional asymmetry: applyGridFilter/applyGridSort never
    // cleared columnDrawer — only applySavedViewGrid did (clearForSavedView).
    expect(s.columnDrawer).not.toBeNull();
  });

  it('clearForSavedView (= former applySavedViewGrid detail half): clears rowDrawer + columnDrawer, NOT childFilter', () => {
    const { store, openRow, openColumn, setChildFilter, clearForSavedView } = createDetailStore();
    openRow(row('r1'));
    openColumn(column('c1'));
    setChildFilter({ sheetId: 's1', parentSheetName: 'p', parentRowId: '1', parentRowIndex: 0, count: 1 });
    clearForSavedView();
    const s = store.get();
    expect(s.rowDrawer).toBeNull();
    expect(s.columnDrawer).toBeNull();
    expect(s.childFilter).not.toBeNull(); // the documented asymmetry
  });

  it('openHeaderMenu sets the menu and closes the row drawer', () => {
    const { store, openRow, openHeaderMenu } = createDetailStore();
    openRow(row('r1'));
    const headerMenu = { column: column('c1'), columnIndex: 0, bounds: { x: 0, y: 0, width: 1, height: 1 } };
    openHeaderMenu(headerMenu);
    const s = store.get();
    expect(s.headerMenu).toEqual(headerMenu);
    expect(s.rowDrawer).toBeNull();
  });

  it('closeHeaderMenu clears headerMenu and bails (reference-equal) when already null', () => {
    const { store, openHeaderMenu, closeHeaderMenu } = createDetailStore();
    const before = store.get();
    closeHeaderMenu(); // already null
    expect(store.get()).toBe(before);
    openHeaderMenu(
      { column: column('c1'), columnIndex: 0, bounds: { x: 0, y: 0, width: 1, height: 1 } },
    );
    closeHeaderMenu();
    expect(store.get().headerMenu).toBeNull();
  });

  it('clearRowDrawer (= afterHistoryChange / evidence-viewer companion-open ad hoc patches) clears ONLY rowDrawer', () => {
    const { store, openRow, openColumn, clearRowDrawer } = createDetailStore();
    openColumn(column('c1'));
    openRow(row('r1'));
    clearRowDrawer();
    expect(store.get().rowDrawer).toBeNull();
    // openRow already cleared columnDrawer as a side effect of opening a row —
    // reseed it to prove clearRowDrawer itself leaves an OPEN columnDrawer alone.
    openColumn(column('c2'));
    const before = store.get();
    clearRowDrawer(); // already null — bails
    expect(store.get()).toBe(before);
    expect(store.get().columnDrawer).toEqual(column('c2'));
  });

  it('setProposalInspect sets/clears the Copilot proposal-inspect state', () => {
    const { store, setProposalInspect } = createDetailStore();
    const proposal = {
      seq: 1,
      title: 't',
      spec: {
        action_id: 'map.classify',
        scope: { kind: 'sheet_rows' as const, sheet_id: 1 },
        output_names: { topic: 'Topic' },
        params: { source: ['story'], fields: [{ name: 'topic', type: 'category', labels: ['news', 'other'] }] },
      },
    };
    setProposalInspect(proposal);
    expect(store.get().proposalInspect).toEqual(proposal);
    setProposalInspect(null);
    expect(store.get().proposalInspect).toBeNull();
  });
});
