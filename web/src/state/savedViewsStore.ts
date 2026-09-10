// Owns the sheet-scoped saved-view list, create/rename editor, and explicit
// definition/delete confirmations. It does not own the grid state that a
// saved view describes, nor does it perform persistence: the workspace
// controller supplies both.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';
import type { SavedView } from '../api/open';

export type SavedViewsSheetId = string | number | null;

export type SavedViewsEditor =
  | { kind: 'create' }
  | { kind: 'edit'; view: SavedView };

const savedViewsEditorMutationTicket = Symbol('savedViewsEditorMutationTicket');

/**
 * A store-owned capability for one editor submission. Controllers may retain
 * and return it, but cannot construct a ticket for a different editor.
 */
export type SavedViewsEditorMutationTicket = {
  readonly [savedViewsEditorMutationTicket]: true;
};

export interface SavedViewsPublication {
  viewId: SavedView['id'];
  /** The affected row's name survives a deletion publication. */
  viewName: string;
  /** Where the panel should restore focus after this publication. */
  focusViewId: SavedView['id'] | null;
  action: 'created' | 'renamed' | 'updated' | 'deleted';
  token: number;
}

export type SavedViewsConfirmation =
  | { kind: 'update-definition'; view: SavedView }
  | { kind: 'delete'; view: SavedView };

export interface SavedViewsState {
  activeSheetId: SavedViewsSheetId;
  views: SavedView[];
  viewName: string;
  editor: SavedViewsEditor | null;
  /** A current editor submission is in flight; its controls must stay inert. */
  isSaving: boolean;
  /** Store-owned so an in-flight confirmation survives a Discover remount. */
  confirmation: SavedViewsConfirmation | null;
  /** A failed confirmation request remains visible in its native dialog. */
  confirmationError: string | null;
  /** The latest accepted editor publication, for the list's success feedback. */
  lastPublication: SavedViewsPublication | null;
}

export function createSavedViewsState(): SavedViewsState {
  return {
    activeSheetId: null,
    views: [],
    viewName: '',
    editor: null,
    isSaving: false,
    confirmation: null,
    confirmationError: null,
    lastPublication: null,
  };
}

export function createSavedViewsStore(): {
  store: Store<SavedViewsState>;
  resetForSheet(sheetId: SavedViewsSheetId): void;
  beginViewsLoad(sheetId: SavedViewsSheetId): number;
  isViewsLoadCurrent(sheetId: SavedViewsSheetId, token: number): boolean;
  receiveViews(sheetId: SavedViewsSheetId, token: number, views: SavedView[]): void;
  startCreating(): void;
  setViewName(name: string): void;
  startEditingView(view: SavedView): void;
  cancelEditor(): void;
  beginEditorMutation(): SavedViewsEditorMutationTicket | null;
  completeEditorMutation(ticket: SavedViewsEditorMutationTicket, saved: SavedView): void;
  failEditorMutation(ticket: SavedViewsEditorMutationTicket): void;
  openDefinitionUpdate(view: SavedView): void;
  openDeleteConfirmation(view: SavedView): void;
  cancelConfirmation(): void;
  beginConfirmationMutation(): SavedViewsEditorMutationTicket | null;
  completeDefinitionUpdate(ticket: SavedViewsEditorMutationTicket, saved: SavedView): void;
  completeDeleteMutation(ticket: SavedViewsEditorMutationTicket): void;
  failConfirmationMutation(ticket: SavedViewsEditorMutationTicket, message: string): void;
  consumePublication(token: number): void;
  removeView(viewId: SavedView['id']): void;
} {
  const store = createStore<SavedViewsState>(createSavedViewsState());
  let nextViewsLoadToken = 0;
  let activeViewsLoadToken: number | null = null;
  let nextEditorToken = 0;
  let nextPublicationToken = 0;
  let activeEditorToken = 0;
  let activeEditorMutation: {
    ticket: SavedViewsEditorMutationTicket;
    sheetId: SavedViewsSheetId;
    editorToken: number;
    editor: SavedViewsEditor | null;
    confirmation: SavedViewsConfirmation | null;
  } | null = null;

  const replaceEditor = (editor: SavedViewsEditor | null, viewName: string) => {
    // Opening or closing an editor revokes an older request's authority to
    // change whichever editor now occupies this sheet.
    activeEditorMutation = null;
    activeEditorToken = ++nextEditorToken;
    store.set((state) => ({
      ...state,
      editor,
      viewName,
      isSaving: false,
      confirmation: null,
      confirmationError: null,
    }));
  };

  const ownsEditorMutation = (ticket: SavedViewsEditorMutationTicket) => {
    const mutation = activeEditorMutation;
    const state = store.get();
    return mutation !== null
      && mutation.ticket === ticket
      && state.activeSheetId === mutation.sheetId
      && activeEditorToken === mutation.editorToken
      && (state.editor !== null || state.confirmation !== null);
  };

  const publish = (
    state: SavedViewsState,
    action: SavedViewsPublication['action'],
    view: SavedView,
    focusViewId: SavedView['id'] | null = view.id,
  ): SavedViewsState => ({
    ...state,
    lastPublication: {
      viewId: view.id,
      viewName: view.name,
      focusViewId,
      action,
      token: ++nextPublicationToken,
    },
  });

  return {
    store,

    resetForSheet(sheetId) {
      // Invalidate any in-flight response, including one for this same sheet
      // after an A -> B -> A return.
      activeViewsLoadToken = null;
      activeEditorMutation = null;
      activeEditorToken = ++nextEditorToken;
      store.set({
        activeSheetId: sheetId,
        views: [],
        viewName: '',
        editor: null,
        isSaving: false,
        confirmation: null,
        confirmationError: null,
        lastPublication: null,
      });
    },

    beginViewsLoad(sheetId) {
      const token = ++nextViewsLoadToken;
      if (store.get().activeSheetId === sheetId) activeViewsLoadToken = token;
      return token;
    },

    isViewsLoadCurrent(sheetId, token) {
      return store.get().activeSheetId === sheetId && activeViewsLoadToken === token;
    },

    receiveViews(sheetId, token, views) {
      if (store.get().activeSheetId !== sheetId || activeViewsLoadToken !== token) return;
      store.set((state) => ({ ...state, views }));
    },

    startCreating() {
      replaceEditor({ kind: 'create' }, '');
    },

    setViewName(name) {
      store.set((state) => (
        state.isSaving || state.viewName === name ? state : { ...state, viewName: name }
      ));
    },

    startEditingView(view) {
      replaceEditor({ kind: 'edit', view }, view.name);
    },

    cancelEditor() {
      replaceEditor(null, '');
    },

    beginEditorMutation() {
      const state = store.get();
      if (!state.editor || state.isSaving || state.activeSheetId === null) return null;

      const ticket = { [savedViewsEditorMutationTicket]: true } as SavedViewsEditorMutationTicket;
      activeEditorMutation = {
        ticket,
        sheetId: state.activeSheetId,
        editorToken: activeEditorToken,
        editor: state.editor,
        confirmation: null,
      };
      store.set((current) => ({ ...current, isSaving: true }));
      return ticket;
    },

    completeEditorMutation(ticket, saved) {
      if (!ownsEditorMutation(ticket)) return;
      const mutation = activeEditorMutation;
      const editor = mutation?.editor;
      if (!editor) return;
      activeEditorMutation = null;
      // A list requested before this mutation cannot publish over its result.
      activeViewsLoadToken = null;
      store.set((state) => publish({
        ...state,
        views: editor.kind === 'create'
          ? [saved, ...state.views.filter((view) => view.id !== saved.id)]
          : state.views.map((view) => (view.id === saved.id ? saved : view)),
        editor: null,
        viewName: '',
        isSaving: false,
      }, editor.kind === 'create' ? 'created' : 'renamed', saved));
    },

    failEditorMutation(ticket) {
      if (!ownsEditorMutation(ticket)) return;
      activeEditorMutation = null;
      store.set((state) => ({ ...state, isSaving: false }));
    },

    openDefinitionUpdate(view) {
      const state = store.get();
      if (state.isSaving || state.activeSheetId === null) return;
      activeEditorMutation = null;
      activeEditorToken = ++nextEditorToken;
      store.set((current) => ({
        ...current,
        editor: null,
        viewName: '',
        confirmation: { kind: 'update-definition', view },
        confirmationError: null,
      }));
    },

    openDeleteConfirmation(view) {
      const state = store.get();
      if (state.isSaving || state.activeSheetId === null) return;
      activeEditorMutation = null;
      activeEditorToken = ++nextEditorToken;
      store.set((current) => ({
        ...current,
        editor: null,
        viewName: '',
        confirmation: { kind: 'delete', view },
        confirmationError: null,
      }));
    },

    cancelConfirmation() {
      const state = store.get();
      if (state.isSaving) return;
      activeEditorMutation = null;
      activeEditorToken = ++nextEditorToken;
      store.set((current) => ({ ...current, confirmation: null, confirmationError: null }));
    },

    beginConfirmationMutation() {
      const state = store.get();
      if (!state.confirmation || state.isSaving || state.activeSheetId === null) return null;
      const ticket = { [savedViewsEditorMutationTicket]: true } as SavedViewsEditorMutationTicket;
      activeEditorMutation = {
        ticket,
        sheetId: state.activeSheetId,
        editorToken: activeEditorToken,
        editor: null,
        confirmation: state.confirmation,
      };
      store.set((current) => ({ ...current, isSaving: true, confirmationError: null }));
      return ticket;
    },

    completeDefinitionUpdate(ticket, saved) {
      if (!ownsEditorMutation(ticket)) return;
      const mutation = activeEditorMutation;
      if (!mutation || mutation.confirmation?.kind !== 'update-definition') return;
      activeEditorMutation = null;
      activeViewsLoadToken = null;
      store.set((state) => publish({
        ...state,
        views: state.views.map((view) => (view.id === saved.id ? saved : view)),
        isSaving: false,
        confirmation: null,
        confirmationError: null,
      }, 'updated', saved));
    },

    completeDeleteMutation(ticket) {
      if (!ownsEditorMutation(ticket)) return;
      const mutation = activeEditorMutation;
      if (!mutation || mutation.confirmation?.kind !== 'delete') return;
      activeEditorMutation = null;
      activeViewsLoadToken = null;
      const deleted = mutation.confirmation.view;
      store.set((state) => {
        const index = state.views.findIndex((view) => view.id === deleted.id);
        const focusViewId = state.views[index + 1]?.id ?? state.views[index - 1]?.id ?? null;
        return publish({
          ...state,
          views: state.views.filter((view) => view.id !== deleted.id),
          isSaving: false,
          confirmation: null,
          confirmationError: null,
        }, 'deleted', deleted, focusViewId);
      });
    },

    failConfirmationMutation(ticket, message) {
      if (!ownsEditorMutation(ticket)) return;
      activeEditorMutation = null;
      store.set((state) => ({ ...state, isSaving: false, confirmationError: message }));
    },

    consumePublication(token) {
      store.set((state) => (
        state.lastPublication?.token === token
          ? { ...state, lastPublication: null }
          : state
      ));
    },

    removeView(viewId) {
      // Deletion is a local publication too; retire a pre-delete list request.
      activeViewsLoadToken = null;
      store.set((state) => {
        const view = state.views.find((candidate) => candidate.id === viewId);
        if (!view) return state;
        const index = state.views.findIndex((candidate) => candidate.id === viewId);
        const focusViewId = state.views[index + 1]?.id ?? state.views[index - 1]?.id ?? null;
        return publish({
          ...state,
          views: state.views.filter((candidate) => candidate.id !== viewId),
        }, 'deleted', view, focusViewId);
      });
    },
  };
}

export type SavedViewsStoreHandle = ReturnType<typeof createSavedViewsStore>;
