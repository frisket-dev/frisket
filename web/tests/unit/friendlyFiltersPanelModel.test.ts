import { describe, expect, it } from 'vitest';

import {
  activeEqValue,
  valueFacetSummary,
} from '../../src/components/friendlyFiltersPanelModel';
import type { ColumnValuesPreview, GridFilterSpec } from '../../src/api/open';



describe('active filter readback', () => {
  const filter: GridFilterSpec = { body: { contains: 'jane doe' }, status: { eq: 'open' } };

  it('reads the eq value for a value facet', () => {
    expect(activeEqValue(filter, 'status')).toBe('open');
    expect(activeEqValue(filter, 'body')).toBeNull();
    expect(activeEqValue(null, 'status')).toBeNull();
  });
});

function preview(overrides: Partial<ColumnValuesPreview> = {}): ColumnValuesPreview {
  return {
    sheetId: '7',
    columnId: '11',
    inputColumn: 'status',
    totalRows: 100,
    distinct: 3,
    missing: 0,
    values: [
      { value: 'open', count: 60 },
      { value: 'closed', count: 40 },
    ],
    offset: 0,
    limit: 500,
    truncated: false,
    valueHash: 'abc',
    search: null,
    ...overrides,
  };
}

describe('valueFacetSummary', () => {
  it('reports the full distinct count when nothing is truncated', () => {
    expect(valueFacetSummary(preview({ distinct: 2 }))).toBe('2 distinct values');
  });

  it('reports top N of M honestly when truncated', () => {
    expect(valueFacetSummary(preview({ distinct: 5000, truncated: true }))).toBe(
      'Top 2 of 5,000 distinct values',
    );
  });

  it('reports top N of M when the page is shorter than the distinct total', () => {
    expect(valueFacetSummary(preview({ distinct: 9 }))).toBe('Top 2 of 9 distinct values');
  });

  it('appends the blank-cell tally', () => {
    expect(valueFacetSummary(preview({ distinct: 2, missing: 3 }))).toBe(
      '2 distinct values · 3 blank cells',
    );
  });

  it('handles an empty column', () => {
    expect(valueFacetSummary(preview({ distinct: 0, values: [] }))).toBe(
      'No values in this column.',
    );
  });

  it('counts matches (not the whole column) while a search is active', () => {
    // `distinct`/`missing` stay UNFILTERED, so the searching line must not
    // reuse the "N distinct values" phrasing or the blank tally.
    expect(
      valueFacetSummary(preview({ distinct: 5000, missing: 12, search: 'ac' })),
    ).toBe('2 matching values of 5,000 distinct');
  });

  it('says "first N" while a search has more matches beyond the page', () => {
    expect(
      valueFacetSummary(preview({ distinct: 5000, search: 'ac', truncated: true })),
    ).toBe('First 2 matches of 5,000 distinct values');
  });

  it('reports a search with no matches without claiming the column is empty', () => {
    expect(valueFacetSummary(preview({ distinct: 5000, values: [], search: 'zzz' }))).toBe(
      'No matches in 5,000 distinct values',
    );
  });
});
