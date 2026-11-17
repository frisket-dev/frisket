import { expect, it } from 'vitest';
import type { SheetMeta, ViewSelection } from '../../src/api/types';
import { gridFromSelection, sameViewDefinition, selectionFromGrid, presentationFromGrid, gridFromPresentation } from '../../src/workspace/viewDefinition';
import { computeRowCacheKey } from '../../src/grid/rowCacheStore';
const sheet = { id: '1', name: 'Contracts', columns: [{ id: '10', name: 'amount' }, { id: '11', name: 'supplier' }] } as SheetMeta;
const selection: ViewSelection = { scope: { kind: 'sheet', sheet_id: 1, parent_row_id: 8 }, filters: [{ column_id: 10, condition: { gte: '100' } }], order: [{ column_id: 10, dir: 'desc' }], limit: 20, search: { text: 'bridge' } };
it('binds the current names once and retains query, limit and parent scope through browsing edits', () => {
  expect(selectionFromGrid(sheet, { supplier: { eq: 'Acme' } }, [{ column: 'amount', dir: 'asc' }], [], selection)).toEqual({
    ...selection, scope: { kind: 'sheet', sheet_id: 1, row_ids: [], parent_row_id: 8 },
    filters: [{ column_id: 11, condition: { eq: 'Acme' } }], order: [{ column_id: 10, dir: 'asc' }],
  });
});
it('opening a view follows column identity after a rename and refuses deleted identities', () => {
  expect(gridFromSelection({ ...sheet, columns: [{ ...sheet.columns[0], name: 'contract_amount' }] }, selection)).toEqual({ filter: { contract_amount: { gte: '100' } }, sort: [{ column: 'contract_amount', dir: 'desc' }] });
  expect(() => gridFromSelection({ ...sheet, columns: [] }, selection)).toThrow('unavailable');
});
it('marks real browsing edits modified without depending on JSON object key order', () => {
  const a = { selection, presentation: { columns: [10] } };
  expect(sameViewDefinition(a, { presentation: a.presentation, selection: { ...selection } })).toBe(true);
  expect(sameViewDefinition(a, { ...a, selection: { ...selection, limit: 21 } })).toBe(false);
});
it('invalidates cached pages when any selection criterion or saved view identity changes', () => {
  const key = computeRowCacheKey('1', 0, { selection });
  expect(computeRowCacheKey('1', 0, { selection: { ...selection, limit: 21 } })).not.toBe(key);
  expect(computeRowCacheKey('1', 0, { viewId: 4 })).not.toBe(computeRowCacheKey('1', 0, { viewId: 5 }));
});

it('keeps applied criteria bound across rename, deletion and reuse of a display name', async () => {
  const { createGridViewStore } = await import('../../src/state/gridViewStore');
  let current = sheet;
  const grid = createGridViewStore(() => current);
  grid.applyFilter({ filter: { amount: { gte: '100' } } });
  grid.applySort({ column: 'amount', direction: 'desc', sort: [{ column: 'amount', dir: 'desc' }] });
  current = { ...sheet, columns: [{ ...sheet.columns[0], name: 'renamed_amount' }, { ...sheet.columns[1], name: 'amount' }] };
  const read = () => {
    const applied = grid.store.get().applied;
    return selectionFromGrid(current, applied.filter, applied.sort, null, applied.selection, applied);
  };
  expect(read().filters[0].column_id).toBe(10);
  expect(read().order[0]).toEqual({ column_id: 10, dir: 'desc' });
  current = { ...current, columns: [current.columns[1]] };
  expect(read().filters[0].column_id).toBe(10); // Server reports unavailable; never widen or switch.
  grid.clearFilter();
  grid.clearSort();
  grid.applyFilter({ filter: { amount: { gte: '200' } } });
  expect(read().filters[0].column_id).toBe(11); // Explicitly choosing the replacement binds it.
});

it('preserves layout identities through rename and never adopts a replacement name', () => {
  const presentation = presentationFromGrid(sheet, ['amount'], [{ run_id: 4, label: 'Output',
    columns: ['amount', 'supplier'], show_confidence: false, show_justification: true }]);
  expect(presentation.columns).toEqual([10]);
  expect(presentation.column_groups?.[0].columns).toEqual([10, 11]);
  const renamed = { ...sheet, columns: [{ ...sheet.columns[0], name: 'renamed' }, { ...sheet.columns[1], name: 'amount' }] };
  expect(gridFromPresentation(renamed, presentation).columns).toEqual(['renamed']);
  expect(gridFromPresentation({ ...renamed, columns: [renamed.columns[1]] }, presentation)).toEqual({
    columns: [], groups: [{ run_id: 4, label: 'Output', columns: ['amount'], show_confidence: false, show_justification: true }],
  });
});
