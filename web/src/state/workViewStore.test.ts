import { describe, expect, it } from 'vitest';
import { createWorkViewState, createWorkViewStore } from './workViewStore';

describe('createWorkViewStore', () => {
  it('starts with the same defaults the pre-migration scattered state/reducer fields used', () => {
    const { store } = createWorkViewStore();
    expect(store.get()).toEqual(createWorkViewState());
  });

  it('setGridOnlySheetId / setActivePromotedKey bail (reference-equal) on a no-op set', () => {
    const { store, setGridOnlySheetId, setActivePromotedKey } = createWorkViewStore();
    const before = store.get();
    setGridOnlySheetId(null);
    setActivePromotedKey(null);
    expect(store.get()).toBe(before);
    setGridOnlySheetId('s1');
    setActivePromotedKey('map:s1:');
    expect(store.get().gridOnlySheetId).toBe('s1');
    expect(store.get().activePromotedKey).toBe('map:s1:');
  });

  it('sheet change clears only nested Answers choices and preserves work-view activation facts', () => {
    const workView = createWorkViewStore();
    workView.setGridOnlySheetId('s1');
    workView.setActivePromotedKey('map:s1:c1');
    workView.setAnswersViewSheetId('s1');
    workView.setAnswersColumn('c1');
    workView.setAnswersActiveLink(42);

    expect(workView.store.get()).toEqual({
      gridOnlySheetId: 's1',
      activePromotedKey: 'map:s1:c1',
      answersViewSheetId: 's1',
      answersView: { chosenColumnId: 'c1', activeLinkId: 42 },
    });

    workView.resetForSheetChange();

    expect(workView.store.get()).toEqual({
      gridOnlySheetId: 's1',
      activePromotedKey: 'map:s1:c1',
      answersViewSheetId: 's1',
      answersView: { chosenColumnId: null, activeLinkId: null },
    });
  });

  it('creates isolated state for separate project-mounted owners', () => {
    const projectA = createWorkViewStore();
    const projectB = createWorkViewStore();
    projectA.setGridOnlySheetId('sheet-a');
    projectA.setAnswersColumn('column-a');

    expect(projectB.store.get()).toEqual(createWorkViewState());
    expect(projectB.store).not.toBe(projectA.store);
  });
});
