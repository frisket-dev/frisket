// Pins reload-persistence behavior: openSplit, promotedViews, documentView,
// ribbonMode, activeRibbonTab, discoverOpen/discoverTab reload from localStorage;
// actionPanelOpen NEVER restores. The load side is this store's constructor (see
// chromeStore.ts); the save side (localStorage.setItem) lives at
// bind/useWorkspaceChromeState.ts, out of this unit test's scope (a plain store
// unit test can't observe a bind-layer closure) — see that file and the
// reload-persistence e2e specs for the save-side + full round-trip proof.

import { beforeEach, describe, expect, it } from 'vitest';
import { createChromeState, createChromeStore } from './chromeStore';

const PROJECT_ID = 'proj-1';

describe('createChromeStore', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('starts with the same defaults createWorkspaceChromeState used', () => {
    const { store } = createChromeStore(PROJECT_ID);
    expect(store.get()).toEqual(createChromeState(PROJECT_ID));
  });

  it('starts with commandPaletteQuery, provenanceOpen, and overflowMenuOpen at their local-state defaults', () => {
    const { store } = createChromeStore(PROJECT_ID);
    const s = store.get();
    expect(s.commandPaletteQuery).toBe('');
    expect(s.provenanceOpen).toBe(false);
    expect(s.overflowMenuOpen).toBe(false);
  });

  // --- actionPanelOpen: NEVER restores from storage --------------------------
  // The retired storage key (frisket:action-panel-open:*) is no longer
  // registered, read, or swept: the field is
  // session-only in memory, so a stale legacy value is simply ignored.

  it('actionPanelOpen never restores from storage — a stale legacy "open" key is ignored', () => {
    localStorage.setItem(`frisket:action-panel-open:${PROJECT_ID}`, '1');
    const { store } = createChromeStore(PROJECT_ID);
    expect(store.get().actionPanelOpen).toBe(false);
  });

  it('actionPanelOpen open/hide/toggle are plain in-memory transitions (no persistence call is this store\'s concern)', () => {
    const { store, openActionPanel, hideActionPanel, toggleActionPanelOpen } = createChromeStore(PROJECT_ID);
    openActionPanel();
    expect(store.get().actionPanelOpen).toBe(true);
    hideActionPanel();
    expect(store.get().actionPanelOpen).toBe(false);
    toggleActionPanelOpen();
    expect(store.get().actionPanelOpen).toBe(true);
    toggleActionPanelOpen();
    expect(store.get().actionPanelOpen).toBe(false);
  });

  // --- load-transform round-trips: ribbonMode/activeRibbonTab/discover ----

  it('ribbonMode loads "menu" only on an exact stored match, "ribbon" otherwise', () => {
    localStorage.setItem(`frisket:ribbon-mode:${PROJECT_ID}`, 'menu');
    expect(createChromeState(PROJECT_ID).ribbonMode).toBe('menu');
    localStorage.setItem(`frisket:ribbon-mode:${PROJECT_ID}`, 'garbage');
    expect(createChromeState(PROJECT_ID).ribbonMode).toBe('ribbon');
  });

  it('activeRibbonTab loads the stored value or falls back to "home"', () => {
    expect(createChromeState(PROJECT_ID).activeRibbonTab).toBe('home');
    localStorage.setItem(`frisket:ribbon-tab:${PROJECT_ID}`, 'act');
    expect(createChromeState(PROJECT_ID).activeRibbonTab).toBe('act');
  });

  it('discoverTab loads the stored value (first-party OR plugin id, unvalidated) or falls back to "Facets"', () => {
    expect(createChromeState(PROJECT_ID).discoverTab).toBe('Facets');
    localStorage.setItem(`frisket:discover-tab:${PROJECT_ID}`, 'some.plugin.panel');
    expect(createChromeState(PROJECT_ID).discoverTab).toBe('some.plugin.panel');
  });

  it('discoverOpen: an explicit stored preference always wins over the width default', () => {
    localStorage.setItem(`frisket:discover-open:${PROJECT_ID}`, '0');
    expect(createChromeState(PROJECT_ID).discoverOpen).toBe(false);
    localStorage.setItem(`frisket:discover-open:${PROJECT_ID}`, '1');
    expect(createChromeState(PROJECT_ID).discoverOpen).toBe(true);
  });

  // --- load-transform round-trips: openSplit/documentView/promotedViews ---

  it('openSplit round-trips a valid stored split and rejects a malformed one', () => {
    localStorage.setItem(
      `frisket:open-split:${PROJECT_ID}`,
      JSON.stringify({ kind: 'map', sheetId: 's1', columnId: 'c1' }),
    );
    expect(createChromeState(PROJECT_ID).openSplit).toEqual({ kind: 'map', sheetId: 's1', columnId: 'c1' });

    localStorage.setItem(`frisket:open-split:${PROJECT_ID}`, JSON.stringify({ kind: 'bogus' }));
    expect(createChromeState(PROJECT_ID).openSplit).toBeNull();

    localStorage.setItem(`frisket:open-split:${PROJECT_ID}`, 'not json');
    expect(createChromeState(PROJECT_ID).openSplit).toBeNull();
  });

  it('documentView round-trips a valid stored view, filling in defaults for missing optional fields', () => {
    localStorage.setItem(
      `frisket:document-view:${PROJECT_ID}`,
      JSON.stringify({ sheetId: 's1' }),
    );
    expect(createChromeState(PROJECT_ID).documentView).toEqual({
      sheetId: 's1',
      sourceColumnId: null,
      titleColumnId: null,
      layout: 'continuous',
      fit: 'width',
      videoFit: 'full',
      textLayer: true,
      sync: false,
      activeRowId: null,
    });
  });

  it('documentView rejects a stored value with no sheetId', () => {
    localStorage.setItem(`frisket:document-view:${PROJECT_ID}`, JSON.stringify({}));
    expect(createChromeState(PROJECT_ID).documentView).toBeNull();
  });

  it('promotedViews round-trips a valid stored list and filters out malformed entries', () => {
    localStorage.setItem(
      `frisket:promoted-views:${PROJECT_ID}`,
      JSON.stringify([
        { key: 'map:s1:', sheetId: 's1', kind: 'map', label: 'Map of s1' },
        { bogus: true },
      ]),
    );
    expect(createChromeState(PROJECT_ID).promotedViews).toEqual([
      { key: 'map:s1:', sheetId: 's1', kind: 'map', label: 'Map of s1' },
    ]);
  });

  // --- setters: commandPaletteQuery / provenanceOpen / overflowMenuOpen --
  // --- (fields moved in from a sibling reducer) ---------------------------

  it('setCommandPaletteQuery sets; closeCommandPaletteAndReset clears the query AND closes the palette in one call', () => {
    const { store, openCommandPalette, setCommandPaletteQuery, closeCommandPaletteAndReset } =
      createChromeStore(PROJECT_ID);
    openCommandPalette();
    setCommandPaletteQuery('act');
    expect(store.get().commandPaletteQuery).toBe('act');
    expect(store.get().commandPaletteOpen).toBe(true);
    closeCommandPaletteAndReset();
    expect(store.get().commandPaletteQuery).toBe('');
    expect(store.get().commandPaletteOpen).toBe(false);
  });

  it('toggleProvenanceOpen flips; closeProvenanceOpen forces closed', () => {
    const { store, toggleProvenanceOpen, closeProvenanceOpen } = createChromeStore(PROJECT_ID);
    toggleProvenanceOpen();
    expect(store.get().provenanceOpen).toBe(true);
    toggleProvenanceOpen();
    expect(store.get().provenanceOpen).toBe(false);
    toggleProvenanceOpen();
    closeProvenanceOpen();
    expect(store.get().provenanceOpen).toBe(false);
  });

  it('setOverflowMenuOpen sets explicitly', () => {
    const { store, setOverflowMenuOpen } = createChromeStore(PROJECT_ID);
    setOverflowMenuOpen(true);
    expect(store.get().overflowMenuOpen).toBe(true);
    setOverflowMenuOpen(false);
    expect(store.get().overflowMenuOpen).toBe(false);
  });

  // --- everyday reducer-parity actions -------------------------------------

  it('openEvidenceViewer defaults host to modalOrPeek; closeEvidenceViewer clears', () => {
    const { store, openEvidenceViewer, closeEvidenceViewer } = createChromeStore(PROJECT_ID);
    openEvidenceViewer('link-1');
    expect(store.get().evidenceViewerState).toEqual({ linkId: 'link-1', host: 'modalOrPeek' });
    openEvidenceViewer('link-2', 'mainView');
    expect(store.get().evidenceViewerState).toEqual({ linkId: 'link-2', host: 'mainView' });
    closeEvidenceViewer();
    expect(store.get().evidenceViewerState).toBeNull();
  });

  it('setDeleteRowsConfirm/clearDeleteRowsConfirm', () => {
    const { store, setDeleteRowsConfirm, clearDeleteRowsConfirm } = createChromeStore(PROJECT_ID);
    setDeleteRowsConfirm({ sheetId: 's1', rowIds: ['r1'] });
    expect(store.get().deleteRowsConfirm).toEqual({ sheetId: 's1', rowIds: ['r1'] });
    clearDeleteRowsConfirm();
    expect(store.get().deleteRowsConfirm).toBeNull();
  });

  it('setError stores the toast error verbatim (timer/auto-clear is bind-layer showError\'s concern, not this store\'s)', () => {
    const { store, setError } = createChromeStore(PROJECT_ID);
    setError({ message: 'boom', code: 'E1' });
    expect(store.get().error).toEqual({ message: 'boom', code: 'E1' });
    setError(null);
    expect(store.get().error).toBeNull();
  });

  it('recordCommandAction sets lastCommandAction', () => {
    const { store, recordCommandAction } = createChromeStore(PROJECT_ID);
    recordCommandAction('Opened Sources');
    expect(store.get().lastCommandAction).toBe('Opened Sources');
  });

  it('openCopilotPopover/closeCopilotPopover/toggleCopilotPopover', () => {
    const { store, openCopilotPopover, closeCopilotPopover, toggleCopilotPopover } =
      createChromeStore(PROJECT_ID);
    openCopilotPopover();
    expect(store.get().copilotPopoverOpen).toBe(true);
    closeCopilotPopover();
    expect(store.get().copilotPopoverOpen).toBe(false);
    toggleCopilotPopover();
    expect(store.get().copilotPopoverOpen).toBe(true);
  });

  it('setOpenSplit/setDocumentView/setPromotedViews are plain in-memory setters (no persistence call is this store\'s concern)', () => {
    const { store, setOpenSplit, setDocumentView, setPromotedViews } = createChromeStore(PROJECT_ID);
    setOpenSplit({ kind: 'graph', sheetId: 's1' });
    expect(store.get().openSplit).toEqual({ kind: 'graph', sheetId: 's1' });
    setDocumentView({
      sheetId: 's1',
      sourceColumnId: null,
      titleColumnId: null,
      layout: 'continuous',
      fit: 'width',
      videoFit: 'full',
      textLayer: true,
      sync: true,
      activeRowId: null,
    });
    expect(store.get().documentView?.sheetId).toBe('s1');
    setPromotedViews([{ key: 'graph:s1:', sheetId: 's1', kind: 'graph', label: 'Graph of s1' }]);
    expect(store.get().promotedViews).toHaveLength(1);
    // No localStorage write from the raw store method — bind/
    // useWorkspaceChromeState.ts's wrapped setters own that write.
    expect(localStorage.getItem(`frisket:open-split:${PROJECT_ID}`)).toBeNull();
  });

  it('toggleDiscoverOpen flips discoverOpen', () => {
    const { store, toggleDiscoverOpen } = createChromeStore(PROJECT_ID);
    const before = store.get().discoverOpen;
    toggleDiscoverOpen();
    expect(store.get().discoverOpen).toBe(!before);
  });
});
