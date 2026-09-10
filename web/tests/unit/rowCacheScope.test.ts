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

import { computeRowCacheKey, resolveRowCacheScope } from '../../src/grid/rowCacheStore';

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
