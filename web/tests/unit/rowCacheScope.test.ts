// The invariant that lets a grid filter/sort change stop bumping the
// workspace's `dataVersion` (useWorkspaceModel.tsx).
//
// third-review REAL-BUG: every filter/sort callback ended with
// `updateData({ type: 'bumpDataVersion' })`, so a SCOPE change was
// indistinguishable from a DATA change for everything downstream of the
// signal. The Mentions panel reads a version change as "this extraction
// changed, re-read it from page 1", which meant clicking a mention group threw
// away every page the user had loaded past the first — the group they clicked
// vanished as they clicked it.
//
// The bump was never what refetched the rows: computeRowCacheKey already folds
// the filter, the sort, the parent row and the lens row-ids into the cache's
// reset key, and useRowCache resets and refetches on that key. These tests pin
// that, so the removal cannot be quietly undone by a change to the key.

import { describe, expect, it } from 'vitest';

import { computeRowCacheKey, createRowCacheStore, resolveRowCacheScope } from '../../src/grid/rowCacheStore';

const SHEET = '7';
const VERSION = 3;

describe('computeRowCacheKey — scope changes re-key without a data-version bump', () => {
  it('changes when only the filter changes', () => {
    const none = computeRowCacheKey(SHEET, VERSION, resolveRowCacheScope({ filter: null }));
    const filtered = computeRowCacheKey(
      SHEET,
      VERSION,
      resolveRowCacheScope({ filter: { entities: { entity_eq: { type: 'organization', fingerprint: 'acme corp' } } } }),
    );
    expect(filtered).not.toBe(none);
  });

  it('distinguishes two DIFFERENT mention filters — single-select replaces, and the rows differ', () => {
    const acme = computeRowCacheKey(
      SHEET,
      VERSION,
      resolveRowCacheScope({ filter: { entities: { entity_eq: { type: 'organization', fingerprint: 'acme corp' } } } }),
    );
    const zenith = computeRowCacheKey(
      SHEET,
      VERSION,
      resolveRowCacheScope({ filter: { entities: { entity_eq: { type: 'organization', fingerprint: 'zenith' } } } }),
    );
    expect(acme).not.toBe(zenith);
  });

  it('changes when only the sort changes, and when the filter is cleared again', () => {
    const unsorted = computeRowCacheKey(SHEET, VERSION, resolveRowCacheScope({ sort: null }));
    const sorted = computeRowCacheKey(
      SHEET,
      VERSION,
      resolveRowCacheScope({ sort: [{ column: 'name', dir: 'asc' }] }),
    );
    expect(sorted).not.toBe(unsorted);

    const filtered = computeRowCacheKey(
      SHEET,
      VERSION,
      resolveRowCacheScope({ filter: { status: { eq: 'open' } } }),
    );
    expect(computeRowCacheKey(SHEET, VERSION, resolveRowCacheScope({ filter: null })))
      .not.toBe(filtered);
  });

  it('still re-keys on a real data change at an unchanged scope', () => {
    const scope = resolveRowCacheScope({ filter: { status: { eq: 'open' } } });
    expect(computeRowCacheKey(SHEET, VERSION + 1, scope))
      .not.toBe(computeRowCacheKey(SHEET, VERSION, scope));
  });
});

 it('keeps projected and bounded row caches separate from exact/full rows', () => {
  const full = computeRowCacheKey(SHEET, VERSION);
  const first = computeRowCacheKey(SHEET, VERSION, { columnIds: ['1'], valueMode: 'bounded' });
  expect(first).not.toBe(full);
  expect(first).not.toBe(computeRowCacheKey(SHEET, VERSION, { columnIds: ['2'], valueMode: 'bounded' }));
  expect(first).not.toBe(computeRowCacheKey(SHEET, VERSION, { columnIds: ['1'], valueMode: 'exact' }));
  expect(computeRowCacheKey(SHEET, VERSION, { columnIds: ['2', '1'] }))
    .toBe(computeRowCacheKey(SHEET, VERSION, { columnIds: ['1', '2'] }));
});

it('retains bounded header metadata only for the active row projection', () => {
  const slot = createRowCacheStore().getSlot(SHEET);
  slot.activate('first');
  const epoch = slot.beginEpoch('first');
  const columns = [{ id: '1', name: 'Text', type: 'text', replayPendingCount: null, replayComparisonAvailable: false }];
  slot.setPage('first', epoch, 0, [], 0, columns);
  expect(slot.getSnapshot().columns?.[0].replayComparisonAvailable).toBe(false);
  slot.activate('second');
  expect(slot.getSnapshot().columns).toBeUndefined();
});
