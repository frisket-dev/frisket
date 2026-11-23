import { describe, expect, it } from 'vitest';
import type { ColumnDef, ViewSelection } from '../../src/api/types';
import {
  chipSummary,
  deriveRowFilterChips,
  facetColumnsOf,
  isFacetColumn,
  limitNote,
  searchOf,
  searchScopeColumnsOf,
  selectionWithoutFacet,
  sheetCountLabel,
  withColumnContains,
  withColumnFacet,
  withSearch,
  type RowFilterState,
} from '../../src/workspace/rowFiltering';
import { sameViewSelection } from '../../src/workspace/viewDefinition';

function selection(overrides: Partial<ViewSelection> = {}): ViewSelection {
  return { scope: { kind: 'sheet', sheet_id: 1 }, filters: [], order: [], limit: null, ...overrides };
}

function state(overrides: Partial<RowFilterState> = {}): RowFilterState {
  return { selection: selection(), filter: null, filterValueLabel: null, sort: null, scopeRowIds: null, ...overrides };
}

const column = (name: string, type: ColumnDef['type'], extra: Partial<ColumnDef> = {}): ColumnDef => ({
  id: name, name, type, ...extra,
});

describe('row filter chips', () => {
  it('shows nothing, and a zero badge, when nothing is active', () => {
    expect(deriveRowFilterChips(state())).toEqual([]);
    expect(chipSummary([])).toBe('No filters');
  });

  it('puts filter chips before shaping chips, one chip per constraint', () => {
    const chips = deriveRowFilterChips(state({
      selection: selection({ search: { text: 'the' }, limit: 3 }),
      filter: { filename: { in: ['a', 'b', 'c'] }, size: { between: { start: '2.0', end: '2.5' } } },
      sort: [{ column: 'size', dir: 'desc' }],
    }));
    expect(chips.map((chip) => [chip.tone, chip.kind, chip.value])).toEqual([
      ['filter', 'Search', '“the”'],
      ['filter', 'filename', '3 selected'],
      ['filter', 'size', '2.0–2.5'],
      ['shape', 'Sorted by', 'size ↓'],
      ['shape', 'Showing', 'first 3'],
    ]);
    expect(chipSummary(chips)).toBe('Search “the” · filename 3 selected · size 2.0–2.5 · Sorted by size ↓ · Showing first 3');
  });

  it('names a scoped search and a single facet value, truncating long values', () => {
    const chips = deriveRowFilterChips(state({
      filter: {
        ocr_text: { contains: 'the' },
        filename: { eq: 'RV-CV-2026-00502_water-alliance_records-request.pdf' },
      },
    }));
    expect(chips.map((chip) => `${chip.kind} ${chip.value}`)).toEqual([
      'Search “the” in ocr_text',
      'filename RV-CV-2026-00502_water-…',
    ]);
    expect(chips[1].target).toEqual({ kind: 'facet', column: 'filename' });
  });

  it('keeps row scope, similarity and score order as chips', () => {
    const chips = deriveRowFilterChips(state({
      selection: selection({ similarity: { index_id: 1 }, order: [{ score: 'similarity', dir: 'desc' }] }),
      scopeRowIds: [4, 5],
    }));
    expect(chips.map((chip) => chip.key)).toEqual(['rows', 'similarity', 'sort']);
    expect(chips[2].value).toBe('similarity score');
  });
});

describe('composed searches from a saved View', () => {
  it('keeps a column contains visible beside a full-text search', () => {
    const chips = deriveRowFilterChips(state({
      selection: selection({ search: { text: 'Northstar' } }),
      filter: { body: { contains: 'invoice' } },
    }));
    expect(chips.map((chip) => `${chip.kind} ${chip.value}`)).toEqual([
      'Search “Northstar”',
      'body contains “invoice”',
    ]);
  });

  it('represents one column contains as the search and keeps the others as constraints', () => {
    const current = { selection: selection(), filter: { body: { contains: 'a' }, title: { contains: 'b', eq: 'x' } } };
    expect(searchOf(current)).toEqual({ text: 'a', scope: 'body' });
    expect(deriveRowFilterChips(state(current)).map((chip) => `${chip.kind} ${chip.value}`)).toEqual([
      'Search “a” in body',
      'title x',
      'title contains “b”',
    ]);
  });

  it('edits or clears only the represented search predicate', () => {
    const fullText = { selection: selection({ search: { text: 'Northstar' } }), filter: { body: { contains: 'invoice' } } };
    expect(withSearch(fullText, { text: 'Acme', scope: 'all' }))
      .toEqual({ selectionSearch: { text: 'Acme' }, filter: { body: { contains: 'invoice' } } });
    expect(withSearch(fullText, { text: '', scope: 'all' }))
      .toEqual({ selectionSearch: undefined, filter: { body: { contains: 'invoice' } } });
    const twoColumns = { selection: selection(), filter: { body: { contains: 'a' }, title: { contains: 'b' } } };
    expect(withSearch(twoColumns, { text: 'c', scope: 'body' }))
      .toEqual({ selectionSearch: undefined, filter: { body: { contains: 'c' }, title: { contains: 'b' } } });
    expect(withSearch(twoColumns, { text: '', scope: 'body' }))
      .toEqual({ selectionSearch: undefined, filter: { title: { contains: 'b' } } });
  });
});

describe('removing one composed constraint', () => {
  it('drops only that column contains and keeps its facet', () => {
    expect(withColumnContains({ body: { contains: 'a', eq: 'x' }, title: { contains: 'b' } }, 'body', null))
      .toEqual({ body: { eq: 'x' }, title: { contains: 'b' } });
    expect(withColumnContains({ title: { contains: 'b' } }, 'title', null)).toBeNull();
  });

  it('offers the search no column that already holds another contains', () => {
    const columns = [column('body', 'text'), column('title', 'text'), column('notes', 'text')];
    const current = { selection: selection(), filter: { body: { contains: 'a' }, title: { contains: 'b' } } };
    expect(searchScopeColumnsOf(columns, current).map((item) => item.name)).toEqual(['body', 'notes']);
  });
});

describe('search scope', () => {
  it('reads all-text search from the selection and a column search from its contains condition', () => {
    expect(searchOf({ selection: selection({ search: { text: 'x' } }), filter: null })).toEqual({ text: 'x', scope: 'all' });
    expect(searchOf({ selection: selection(), filter: { body: { contains: 'y', eq: 'z' } } })).toEqual({ text: 'y', scope: 'body' });
    expect(searchOf({ selection: null, filter: null })).toEqual({ text: '', scope: 'all' });
  });

  it('moves search between its two homes and clears the old one', () => {
    const scoped = withSearch({ selection: selection({ search: { text: 'old' } }), filter: { body: { eq: 'kept' } } }, { text: 'new', scope: 'body' });
    expect(scoped).toEqual({ selectionSearch: undefined, filter: { body: { eq: 'kept', contains: 'new' } } });
    const all = withSearch({ selection: selection(), filter: { body: { eq: 'kept', contains: 'old' } } }, { text: 'new', scope: 'all' });
    expect(all).toEqual({ selectionSearch: { text: 'new' }, filter: { body: { eq: 'kept' } } });
    expect(withSearch({ selection: selection(), filter: { body: { contains: 'old' } } }, { text: '  ', scope: 'body' }))
      .toEqual({ selectionSearch: undefined, filter: null });
  });

  it('replaces a facet without disturbing the same column’s search', () => {
    expect(withColumnFacet({ body: { contains: 'x', eq: 'a' } }, 'body', { in: ['a', 'b'] }))
      .toEqual({ body: { in: ['a', 'b'], contains: 'x' } });
    expect(withColumnFacet({ body: { eq: 'a' } }, 'body', null)).toBeNull();
  });
});

describe('facet logic', () => {
  it('counts a facet within every other condition, without its own, the limit or the order', () => {
    const current = selection({
      filters: [
        { column_id: 1, condition: { in: ['a', 'b'] } },
        { column_id: 2, condition: { eq: 'x', contains: 'needle' } },
      ],
      order: [{ column_id: 1, dir: 'asc' }],
      limit: 5,
      search: { text: 'the' },
    });
    expect(selectionWithoutFacet(current, 2)).toEqual(selection({
      filters: [
        { column_id: 1, condition: { in: ['a', 'b'] } },
        { column_id: 2, condition: { contains: 'needle' } },
      ],
      search: { text: 'the' },
    }));
    expect(selectionWithoutFacet(current, 1).filters.map((item) => item.column_id)).toEqual([2]);
  });

  it('facets text values, including generated ones, and searches markdown', () => {
    expect(isFacetColumn(column('filename', 'text'))).toBe(true);
    expect(isFacetColumn(column('markdown', 'text', { format: 'markdown' }))).toBe(false);
    expect(isFacetColumn(column('label', 'text', { ai: { actionName: 'Classify', prompt: '', model: 'm', costSoFar: 0, versions: [] } }))).toBe(true);
    expect(facetColumnsOf([column('size', 'number'), column('kind', 'category')]).map((item) => item.name))
      .toEqual(['kind', 'size']);
  });
});

describe('counts and the limit row', () => {
  it('reads N rows, or K of N rows while rows are hidden', () => {
    expect(sheetCountLabel(6, 6, 5)).toBe('6 rows · 5 columns');
    expect(sheetCountLabel(3, 6, 5)).toBe('3 of 6 rows · 5 columns');
    expect(sheetCountLabel(1, 1, 1)).toBe('1 row · 1 column');
  });

  it('appears only while the limit hides matching rows', () => {
    expect(limitNote(3, 4)).toEqual({ note: 'Showing first 3 of 4 matching rows.', showAll: 'Show all 4' });
    expect(limitNote(3, 3)).toBeNull();
    expect(limitNote(null, 10)).toBeNull();
    expect(limitNote(3, null)).toBeNull();
  });
});

describe('current view', () => {
  it('matches a saved selection by deep equality, ignoring absent fields', () => {
    const saved = selection({ filters: [{ column_id: 1, condition: { in: ['a'] } }] });
    expect(sameViewSelection(saved, { ...saved, search: undefined, similarity: undefined })).toBe(true);
    expect(sameViewSelection(saved, { ...saved, limit: 3 })).toBe(false);
  });
});
