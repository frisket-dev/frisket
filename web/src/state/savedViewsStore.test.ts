import { describe, expect, it } from 'vitest';
import { createSavedViewsStore, createSavedViewsState } from './savedViewsStore';
import type { SavedView } from '../api/open';

const view = (id: number, name: string, sheetId = 1): SavedView => ({
  id,
  name,
  sheet_id: sheetId,
  spec: { filter: { status: { eq: 'open' } } },
  op_id: null,
});

describe('createSavedViewsStore', () => {
  it('starts in the sheet-neutral list state', () => {
    const { store } = createSavedViewsStore();
    expect(store.get()).toEqual(createSavedViewsState());
  });

  it('has explicit list, create, and edit states; only the mutation owner can publish a save', () => {
    const {
      store,
      resetForSheet,
      beginViewsLoad,
      receiveViews,
      startCreating,
      startEditingView,
      cancelEditor,
      setViewName,
      beginEditorMutation,
      completeEditorMutation,
      consumePublication,
    } = createSavedViewsStore();
    const existing = view(1, 'Open only');

    resetForSheet(1);
    const load = beginViewsLoad(1);
    receiveViews(1, load, [existing]);
    expect(store.get()).toMatchObject({ activeSheetId: 1, editor: null, views: [existing] });

    startCreating();
    setViewName('Draft');
    expect(store.get()).toMatchObject({ editor: { kind: 'create' }, viewName: 'Draft' });

    cancelEditor();
    expect(store.get()).toMatchObject({ editor: null, viewName: '' });

    startEditingView(existing);
    expect(store.get()).toMatchObject({
      editor: { kind: 'edit', view: existing },
      viewName: 'Open only',
    });
    const rename = beginEditorMutation();
    expect(rename).not.toBeNull();
    completeEditorMutation(rename!, view(1, 'Open status'));
    expect(store.get()).toMatchObject({
      editor: null,
      viewName: '',
      views: [view(1, 'Open status')],
    });

    startCreating();
    setViewName('All rows');
    const create = beginEditorMutation();
    expect(create).not.toBeNull();
    completeEditorMutation(create!, view(2, 'All rows'));
    expect(store.get()).toMatchObject({
      editor: null,
      viewName: '',
      views: [view(2, 'All rows'), view(1, 'Open status')],
    });
    const publication = store.get().lastPublication;
    expect(publication).not.toBeNull();
    consumePublication(publication!.token);
    expect(store.get().lastPublication).toBeNull();
    consumePublication(publication!.token - 1);
    expect(store.get().lastPublication).toBeNull();
  });

  it('does not publish a Sheet A create response after the active sheet has changed to Sheet B', () => {
    const {
      store,
      resetForSheet,
      beginViewsLoad,
      receiveViews,
      startCreating,
      setViewName,
      beginEditorMutation,
      completeEditorMutation,
    } = createSavedViewsStore();
    const sheetAResponse = view(1, 'Created on Sheet A', 1);
    const sheetBExisting = view(2, 'Sheet B existing', 2);

    resetForSheet(1);
    const sheetALoad = beginViewsLoad(1);
    receiveViews(1, sheetALoad, []);
    startCreating();
    setViewName('Created on Sheet A');
    const sheetACreate = beginEditorMutation();
    expect(sheetACreate).not.toBeNull();

    // The request was authorized while A was active, but navigation made its
    // response stale before it completed. B's current editor is independent.
    resetForSheet(2);
    const sheetBLoad = beginViewsLoad(2);
    receiveViews(2, sheetBLoad, [sheetBExisting]);
    startCreating();
    setViewName('Sheet B draft');
    completeEditorMutation(sheetACreate!, sheetAResponse);

    expect(store.get()).toMatchObject({
      activeSheetId: 2,
      views: [sheetBExisting],
      editor: { kind: 'create' },
      viewName: 'Sheet B draft',
    });
  });

  it('does not let an older rename response close or overwrite a newer editor', () => {
    const {
      store,
      resetForSheet,
      beginViewsLoad,
      receiveViews,
      startEditingView,
      setViewName,
      beginEditorMutation,
      completeEditorMutation,
      failEditorMutation,
    } = createSavedViewsStore();
    const existing = view(1, 'Original');

    resetForSheet(1);
    const load = beginViewsLoad(1);
    receiveViews(1, load, [existing]);

    startEditingView(existing);
    setViewName('Older rename');
    const olderRename = beginEditorMutation();
    expect(olderRename).not.toBeNull();
    expect(store.get()).toMatchObject({ isSaving: true });

    // A second rename begins before the first response arrives. The older
    // response is not authorized to complete the editor it no longer owns.
    startEditingView(existing);
    setViewName('Newer rename');
    const newerRename = beginEditorMutation();
    expect(newerRename).not.toBeNull();
    completeEditorMutation(olderRename!, view(1, 'Older rename'));
    failEditorMutation(olderRename!);

    expect(store.get()).toMatchObject({
      views: [existing],
      editor: { kind: 'edit', view: existing },
      viewName: 'Newer rename',
      isSaving: true,
    });

    failEditorMutation(newerRename!);
  });

  it('refuses duplicate editor submissions and preserves the editor after its current request fails', () => {
    const { store, resetForSheet, startCreating, setViewName, beginEditorMutation, failEditorMutation } =
      createSavedViewsStore();

    resetForSheet(1);
    startCreating();
    setViewName('Draft');
    const firstAttempt = beginEditorMutation();
    expect(firstAttempt).not.toBeNull();
    expect(beginEditorMutation()).toBeNull();
    expect(store.get()).toMatchObject({ editor: { kind: 'create' }, viewName: 'Draft', isSaving: true });

    failEditorMutation(firstAttempt!);
    expect(store.get()).toMatchObject({ editor: { kind: 'create' }, viewName: 'Draft', isSaving: false });
  });

  it('resets editor and list state for the new sheet and accepts only the current load token', () => {
    const { store, resetForSheet, beginViewsLoad, receiveViews, startEditingView } = createSavedViewsStore();
    const sheetAView = view(1, 'Sheet A', 1);
    const sheetBView = view(2, 'Sheet B', 2);
    const freshSheetAView = view(3, 'Sheet A refreshed', 1);

    resetForSheet(1);
    const firstSheetALoad = beginViewsLoad(1);
    receiveViews(1, firstSheetALoad, [sheetAView]);
    startEditingView(sheetAView);

    resetForSheet(2);
    expect(store.get()).toMatchObject({
      activeSheetId: 2,
      editor: null,
      views: [],
      viewName: '',
    });
    const sheetBLoad = beginViewsLoad(2);
    receiveViews(2, sheetBLoad, [sheetBView]);

    // Returning to A starts another list request. The first A request is now
    // stale even though its sheet id again matches the active sheet.
    resetForSheet(1);
    const freshSheetALoad = beginViewsLoad(1);
    receiveViews(1, firstSheetALoad, [sheetAView]);
    expect(store.get()).toMatchObject({ activeSheetId: 1, editor: null, views: [] });

    receiveViews(1, freshSheetALoad, [freshSheetAView]);
    expect(store.get()).toMatchObject({
      activeSheetId: 1,
      editor: null,
      views: [freshSheetAView],
    });
  });

  it('keeps the Saved Views list sheet-scoped without owning active-grid attribution', () => {
    const { store, resetForSheet, beginViewsLoad, receiveViews, removeView } =
      createSavedViewsStore();
    const sheetAView = view(1, 'Sheet A', 1);
    const replacement = view(2, 'Replacement', 1);

    resetForSheet(1);
    const firstLoad = beginViewsLoad(1);
    receiveViews(1, firstLoad, [sheetAView]);
    expect(store.get()).toMatchObject({ activeSheetId: 1, views: [sheetAView] });

    // A sheet switch must never claim that a view on a different sheet is
    // still the one the user last applied.
    resetForSheet(2);
    expect(store.get()).toMatchObject({ activeSheetId: 2, views: [] });

    resetForSheet(1);
    const refresh = beginViewsLoad(1);
    receiveViews(1, refresh, [sheetAView]);
    removeView(sheetAView.id);
    expect(store.get()).toMatchObject({ views: [] });

    receiveViews(1, refresh, [replacement]);
    const laterRefresh = beginViewsLoad(1);
    receiveViews(1, laterRefresh, [sheetAView]);
    expect(store.get()).toMatchObject({ views: [sheetAView] });
  });

  it('does not let a same-sheet list requested before a mutation overwrite its publication', () => {
    const {
      store,
      resetForSheet,
      beginViewsLoad,
      receiveViews,
      startCreating,
      setViewName,
      beginEditorMutation,
      completeEditorMutation,
      removeView,
    } = createSavedViewsStore();
    const existing = view(1, 'Existing');
    const created = view(2, 'Created');

    resetForSheet(1);
    const initial = beginViewsLoad(1);
    receiveViews(1, initial, [existing]);

    const staleBeforeCreate = beginViewsLoad(1);
    startCreating();
    setViewName('Created');
    const create = beginEditorMutation();
    completeEditorMutation(create!, created);
    receiveViews(1, staleBeforeCreate, [existing]);
    expect(store.get().views).toEqual([created, existing]);

    const staleBeforeDelete = beginViewsLoad(1);
    removeView(created.id);
    receiveViews(1, staleBeforeDelete, [created, existing]);
    expect(store.get().views).toEqual([existing]);
  });

  it('publishes complete definition replacement and ignores a stale same-sheet list', () => {
    const {
      store,
      resetForSheet,
      beginViewsLoad,
      receiveViews,
      openDefinitionUpdate,
      beginConfirmationMutation,
      completeDefinitionUpdate,
    } = createSavedViewsStore();
    const original = view(1, 'Open only');
    const updated = {
      ...view(1, 'Open only'),
      spec: { filter: { status: { eq: 'closed' } }, sort: [{ column: 'city', dir: 'asc' }] },
    };

    resetForSheet(1);
    const initial = beginViewsLoad(1);
    receiveViews(1, initial, [original]);
    const stale = beginViewsLoad(1);
    openDefinitionUpdate(original);
    const ticket = beginConfirmationMutation();
    expect(ticket).not.toBeNull();

    completeDefinitionUpdate(ticket!, updated);
    receiveViews(1, stale, [original]);

    expect(store.get()).toMatchObject({
      views: [updated],
      confirmation: null,
      isSaving: false,
      lastPublication: { action: 'updated', viewId: updated.id, focusViewId: updated.id },
    });
  });

  it('keeps a failed delete confirmation across a panel remount and publishes a stable focus target on success', () => {
    const {
      store,
      resetForSheet,
      beginViewsLoad,
      receiveViews,
      openDeleteConfirmation,
      beginConfirmationMutation,
      failConfirmationMutation,
      completeDeleteMutation,
    } = createSavedViewsStore();
    const first = view(1, 'First');
    const second = view(2, 'Second');

    resetForSheet(1);
    const initial = beginViewsLoad(1);
    receiveViews(1, initial, [first, second]);
    openDeleteConfirmation(first);
    const failedAttempt = beginConfirmationMutation();
    expect(failedAttempt).not.toBeNull();
    failConfirmationMutation(failedAttempt!, 'Network unavailable');

    // The sheet store, rather than a mounted Discover panel, owns this state.
    expect(store.get()).toMatchObject({
      confirmation: { kind: 'delete', view: first },
      confirmationError: 'Network unavailable',
      isSaving: false,
    });

    const successfulAttempt = beginConfirmationMutation();
    expect(successfulAttempt).not.toBeNull();
    completeDeleteMutation(successfulAttempt!);
    expect(store.get()).toMatchObject({
      views: [second],
      confirmation: null,
      lastPublication: { action: 'deleted', viewId: first.id, viewName: first.name, focusViewId: second.id },
    });
  });
});
