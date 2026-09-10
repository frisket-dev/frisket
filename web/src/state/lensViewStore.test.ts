import { describe, expect, it } from 'vitest';
import { createLensViewStore, type LensGridView } from './lensViewStore';

const lens = (sheetId: string): LensGridView => ({
  lensId: 1,
  name: 'l',
  sheetId,
  rowIds: [1, 2],
  scores: {},
  total: 2,
});

describe('lens view', () => {
  it('setLensView / setLensOpenError set independently, matching applyLens’s success patch', () => {
    const { store, setLensView, setLensOpenError } = createLensViewStore();
    setLensOpenError(null); // applyLens clears any stale error at the start
    setLensView(lens('s1'));
    expect(store.get().lensView).toEqual(lens('s1'));
    expect(store.get().lensOpenError).toBeNull();
  });

  it('setLensOpenError alone (= applyLens’s refresh-needed/error branches) sets the message without touching lensView unless also cleared', () => {
    const { store, setLensView, setLensOpenError } = createLensViewStore();
    setLensView(lens('s1'));
    setLensOpenError('stale'); // refresh-needed branch also nulls lensView at the call site
    setLensView(null);
    expect(store.get().lensView).toBeNull();
    expect(store.get().lensOpenError).toBe('stale');
  });

  it('exitLensView (= setLensView(null) + setLensOpenError(null))', () => {
    const { store, setLensView, setLensOpenError } = createLensViewStore();
    setLensView(lens('s1'));
    setLensOpenError('x');
    setLensView(null);
    setLensOpenError(null);
    expect(store.get().lensView).toBeNull();
    expect(store.get().lensOpenError).toBeNull();
  });
});

describe('sheet-scoped session reset', () => {
  it('resetForSheetChange (= former selectSheet reducer case’s scratch half) unconditionally clears lens', () => {
    const { store, setLensView, resetForSheetChange } = createLensViewStore();
    setLensView(lens('s1'));
    resetForSheetChange();
    const s = store.get();
    expect(s.lensView).toBeNull();
    expect(s.lensOpenError).toBeNull();
  });
});
