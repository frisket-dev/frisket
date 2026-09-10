// STRUCTURALLY UNPERSISTABLE AND UNROUTABLE BY DESIGN: every field here is
// per-project work-view session state. This file makes NO localStorage
// calls and imports nothing from state/routeStore.ts — state/ must not import a
// sibling store. Where a callback also touches a chromeStore-owned field
// (setOpenSplit/setDocumentView), that composition stays in the bind layer
// (useWorkspaceModel.tsx) — state/ must not import a sibling store.
//
// createWorkViewStore() is per-project, assembled by state/createWorkspaceStores.ts
// — never a module-level singleton.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';

// The Grounded Answers reading view's own display choice
// (chosen cited column) + the docked pane's current citation link, both
// disposable per-view state -- never persisted, never routed (unlike
// DocumentViewState, which is chrome state, this view's activation itself is
// ALSO work-view-only -- see answersViewSheetId below and chromeStore.ts's
// WorkViewKind comment).
export interface AnswersViewState {
  /** null = default (first cited column in sheet column order). */
  chosenColumnId: string | null;
  /** The docked pane's current source; null = no citation opened yet. */
  activeLinkId: string | number | null;
}

function createAnswersViewState(): AnswersViewState {
  return { chosenColumnId: null, activeLinkId: null };
}

export interface WorkViewState {
  /** Suppresses the ambient gallery pane for this sheet when the user picks
   *  Grid explicitly. */
  gridOnlySheetId: string | null;
  activePromotedKey: string | null;
  /** The sheetId the Grounded Answers view
   *  is showing for -- work-view-only activation, unlike documentView (chrome
   *  state). null = not showing. Compared against the current sheet id the
   *  same way documentViewShowing compares documentView.sheetId. */
  answersViewSheetId: string | null;
  answersView: AnswersViewState;
}

export function createWorkViewState(): WorkViewState {
  return {
    gridOnlySheetId: null,
    activePromotedKey: null,
    answersViewSheetId: null,
    answersView: createAnswersViewState(),
  };
}

export function createWorkViewStore(): {
  store: Store<WorkViewState>;
  setGridOnlySheetId(id: string | null): void;
  setActivePromotedKey(key: string | null): void;

  /** = the 'selectSheet' reducer case's work-view-owned half (unconditional):
   *  answersView resets to its sheet-switch default. */
  resetForSheetChange(): void;

  /** = setWorkView's 'answers' case (via workspaceTransitions.ts's
   *  setWorkViewAnswersTransition): the work-view-only activation flag. */
  setAnswersViewSheetId(sheetId: string | null): void;
  /** The column picker's onChange. */
  setAnswersColumn(columnId: string | null): void;
  /** A citation chip's onOpen, or the docked pane's onClose(null). */
  setAnswersActiveLink(linkId: string | number | null): void;
} {
  const store = createStore<WorkViewState>(createWorkViewState());

  return {
    store,

    setGridOnlySheetId(id) {
      store.set((s) => (s.gridOnlySheetId === id ? s : { ...s, gridOnlySheetId: id }));
    },
    setActivePromotedKey(key) {
      store.set((s) => (s.activePromotedKey === key ? s : { ...s, activePromotedKey: key }));
    },

    resetForSheetChange() {
      store.set((s) => ({
        ...s,
        answersView: createAnswersViewState(),
      }));
    },

    setAnswersViewSheetId(sheetId) {
      store.set((s) =>
        s.answersViewSheetId === sheetId ? s : { ...s, answersViewSheetId: sheetId },
      );
    },
    setAnswersColumn(columnId) {
      store.set((s) => ({ ...s, answersView: { ...s.answersView, chosenColumnId: columnId } }));
    },
    setAnswersActiveLink(linkId) {
      store.set((s) => ({ ...s, answersView: { ...s.answersView, activeLinkId: linkId } }));
    },
  };
}

export type WorkViewStoreHandle = ReturnType<typeof createWorkViewStore>;
