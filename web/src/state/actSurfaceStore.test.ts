// Does not pin run lifecycle (run/actionJobs/costGate) — jobStore owns that; see
// jobStore.test.ts for those cases.

import { describe, expect, it } from 'vitest';
import { createActSurfaceState, createActSurfaceStore } from './actSurfaceStore';

describe('createActSurfaceStore', () => {
  it('starts with the same defaults the pre-migration local-state declarations used', () => {
    const { store } = createActSurfaceStore();
    expect(store.get()).toEqual(createActSurfaceState());
    expect(store.get().actExportTargets).toEqual([]);
    expect(store.get().actExportModal).toBeNull();
    expect(store.get().actionLaunch).toBeNull();
    expect(store.get().importDialogOpen).toBe(false);
    expect(store.get().addColumnPrompt).toBeNull();
  });

  it('setActExportTargets keeps the independently-consumed catalog projection', () => {
    const { store, setActExportTargets } = createActSurfaceStore();
    setActExportTargets([{ kind: 'csv', label: 'CSV' } as never]);
    expect(store.get().actExportTargets).toHaveLength(1);
  });

  it('setActExportModal/closeActExportModal', () => {
    const { store, setActExportModal, closeActExportModal } = createActSurfaceStore();
    setActExportModal('dataset');
    expect(store.get().actExportModal).toBe('dataset');
    setActExportModal('column_tables');
    expect(store.get().actExportModal).toBe('column_tables');
    closeActExportModal();
    expect(store.get().actExportModal).toBeNull();
  });

  it('setActionLaunch (= runActionFromSurface\'s pre-binding half)', () => {
    const { store, setActionLaunch } = createActSurfaceStore();
    setActionLaunch({ kind: 'media.ocr', initial: { sourceColumn: 'file' } });
    expect(store.get().actionLaunch).toEqual({ kind: 'media.ocr', initial: { sourceColumn: 'file' } });
    setActionLaunch(null);
    expect(store.get().actionLaunch).toBeNull();
  });

  it('openImportDialog/closeImportDialog/setImportDialogOpen', () => {
    const {
      store,
      openImportDialog,
      openCsvImport,
      openBulkImport,
      closeImportDialog,
      setImportDialogOpen,
    } = createActSurfaceStore();
    openImportDialog();
    expect(store.get().importDialogOpen).toBe(true);
    closeImportDialog();
    expect(store.get().importDialogOpen).toBe(false);
    const file = new File(['name\nAda\n'], 'people.csv', { type: 'text/csv' });
    const preview = {
      encoding: 'utf-8-sig',
      delimiter: ',',
      decimal_separator: '.',
      row_count: 1,
      columns: [{ name: 'name', type: 'text', format: null }],
      preview_rows: [{ name: 'Ada' }],
      truncated: false,
    };
    openCsvImport(file, preview);
    expect(store.get().importDialogEntryMode).toBe('csv');
    expect(store.get().importDialogCsv).toEqual({ file, preview });
    closeImportDialog();
    expect(store.get().importDialogCsv).toBeNull();
    const files = [new File(['Subject: tip'], 'message.eml', { type: 'message/rfc822' })];
    openBulkImport(files);
    expect(store.get().importDialogEntryMode).toBe('files');
    expect(store.get().importDialogFiles).toEqual(files);
    closeImportDialog();
    expect(store.get().importDialogFiles).toBeNull();
    setImportDialogOpen(true);
    expect(store.get().importDialogOpen).toBe(true);
  });

  it('opens and closes the Sources & connections manager', () => {
    const { store, openSourcesConnections, closeSourcesConnections } = createActSurfaceStore();

    expect(store.get().sourcesConnectionsOpen).toBe(false);
    openSourcesConnections();
    expect(store.get().sourcesConnectionsOpen).toBe(true);
    closeSourcesConnections();
    expect(store.get().sourcesConnectionsOpen).toBe(false);
  });

  it('openAddColumnPromptAt (= the caret-menu simple entry) always sets position null', () => {
    const { store, openAddColumnPromptAt } = createActSurfaceStore();
    openAddColumnPromptAt({ x: 10, y: 20 });
    expect(store.get().addColumnPrompt).toEqual({ position: null, anchor: { x: 10, y: 20 } });
  });

  it('setAddColumnPrompt (= insertColumnBeside\'s computed-position entry)', () => {
    const { store, setAddColumnPrompt } = createActSurfaceStore();
    setAddColumnPrompt({ position: 3, anchor: { x: 1, y: 2 } });
    expect(store.get().addColumnPrompt).toEqual({ position: 3, anchor: { x: 1, y: 2 } });
  });

  it('closeAddColumnPrompt clears (= submitAddColumn success / closeAddColumnPrompt)', () => {
    const { store, openAddColumnPromptAt, closeAddColumnPrompt } = createActSurfaceStore();
    openAddColumnPromptAt({ x: 0, y: 0 });
    closeAddColumnPrompt();
    expect(store.get().addColumnPrompt).toBeNull();
  });
});
