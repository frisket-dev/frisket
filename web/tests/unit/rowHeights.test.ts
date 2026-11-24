import { describe, expect, it } from 'vitest';
import type { Row } from '../../src/api/types';
import { matchRanges, wrappedRowHeights } from '../../src/grid/rowFilterDraw';
import { ROW_HEIGHT, WRAP_MAX_LINES, WRAP_ROW_MAX_HEIGHT, wrapLines, wrappedRowHeight } from '../../src/grid/rowHeights';

const measure = (text: string) => text.length * 10;

describe('wrapped rows', () => {
  it('wraps on words and never past the maximum, ending in an ellipsis', () => {
    expect(wrapLines('one two three', 80, measure)).toEqual(['one two', 'three']);
    const long = Array.from({ length: 200 }, (_, index) => `w${index}`).join(' ');
    const lines = wrapLines(long, 80, measure);
    expect(lines).toHaveLength(WRAP_MAX_LINES);
    expect(lines[WRAP_MAX_LINES - 1].endsWith('…')).toBe(true);
    expect(lines.every((line) => measure(line) <= 80)).toBe(true);
  });

  it('breaks a word wider than the cell and keeps explicit line breaks', () => {
    expect(wrapLines('abcdefghij', 40, measure)).toEqual(['abcd', 'efgh', 'ij']);
    expect(wrapLines('a\nb', 80, measure)).toEqual(['a', 'b']);
  });

  it('grows a row to fit its tallest cell, clamped to the maximum height', () => {
    expect(wrappedRowHeight(1)).toBe(ROW_HEIGHT);
    expect(wrappedRowHeight(3)).toBe(9 * 2 + 3 * 18);
    expect(wrappedRowHeight(10_000)).toBe(WRAP_ROW_MAX_HEIGHT);
  });

  it('measures loaded rows only and keeps unloaded rows one line tall', () => {
    const row: Row = { id: 'r1', index: 0, cells: { c1: 'short', c2: 'one two three four five six seven' }, provenance: {} };
    const heights = wrappedRowHeights((index) => (index === 0 ? row : undefined), [
      { id: 'c1', type: 'text' }, { id: 'c2', type: 'text' },
    ], [200, 116], measure, 8);
    expect(heights(0)).toBe(wrappedRowHeight(4));
    expect(heights(1)).toBe(ROW_HEIGHT);
  });
});

describe('search highlights', () => {
  it('finds every case-insensitive match', () => {
    expect(matchRanges('The other theme', 'the')).toEqual([[0, 3], [5, 8], [10, 13]]);
    expect(matchRanges('abc', '  ')).toEqual([]);
  });
});
