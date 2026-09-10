import { describe, expect, it } from 'vitest';
import type { EntityMentionsCoverage } from '../api/types';
import { coverageLine, uncoveredRows } from './mentionsPanelModel';

const selectedCoverage: EntityMentionsCoverage = {
  targetRows: 2,
  completedRows: 2,
  failedRows: 0,
  sheetRows: 10,
  scopeKind: 'exact_membership',
};

describe('mentions coverage', () => {
  it('does not treat intentional exact membership as sheet drift', () => {
    expect(uncoveredRows(selectedCoverage)).toBe(0);
    expect(coverageLine(selectedCoverage)).toBe('2 of 2 rows extracted');
  });

  it('still reports live-sheet drift for all-rows runs', () => {
    const coverage = { ...selectedCoverage, scopeKind: 'all_rows' as const };
    expect(uncoveredRows(coverage)).toBe(8);
    expect(coverageLine(coverage)).toContain('8 rows on this sheet not extracted');
  });
});
