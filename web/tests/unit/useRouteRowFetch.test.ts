import { describe, expect, it, vi } from 'vitest';
import type { GridApiPort } from '../../src/api/ports';
import type { Row } from '../../src/api/open';
import { fetchRouteRow } from '../../src/bind/useRouteRowFetch';

const row = { id: '7', index: 7, cells: {} } as Row;
const scope = {
  parentRowId: null,
  filter: { status: { eq: 'open' } },
  sort: [{ column: 'name', dir: 'asc' as const }],
  scopeRowIds: [7],
};

function apiWith(
  locateSheetRow: GridApiPort['locateSheetRow'],
  getSheetData: GridApiPort['getSheetData'],
) {
  return { locateSheetRow, getSheetData };
}

describe('fetchRouteRow', () => {
  it('uses the current grid scope when it contains the routed row', async () => {
    const locate = vi.fn().mockResolvedValue({ found: true, index: 7, pageOffset: 0, pageSize: 100 });
    const read = vi.fn().mockResolvedValue({ rows: [row], total: 1 });

    await expect(fetchRouteRow(apiWith(locate, read), 'sheet-1', row.id, scope)).resolves.toEqual(row);
    expect(locate).toHaveBeenCalledOnce();
    expect(read).toHaveBeenCalledWith('sheet-1', 0, 1, { ...scope, rowIds: [7], valueMode: 'bounded' });
  });

  it('falls back to the unfiltered sheet when the current view excludes the row', async () => {
    const locate = vi.fn()
      .mockResolvedValueOnce({ found: false, index: null, pageOffset: null, pageSize: 100 })
      .mockResolvedValueOnce({ found: true, index: 7, pageOffset: 0, pageSize: 100 });
    const read = vi.fn().mockResolvedValue({ rows: [row], total: 1 });

    await expect(fetchRouteRow(apiWith(locate, read), 'sheet-1', row.id, scope)).resolves.toEqual(row);
    expect(locate.mock.calls).toEqual([
      ['sheet-1', row.id, scope],
      ['sheet-1', row.id, null],
    ]);
    expect(read).toHaveBeenCalledWith('sheet-1', 0, 1, { rowIds: [7], valueMode: 'bounded' });
  });

  it('does not repeat a miss when only row ordering is active', async () => {
    const locate = vi.fn().mockResolvedValue({ found: false, index: null, pageOffset: null, pageSize: 100 });
    const read = vi.fn();
    const unscoped = {
      parentRowId: null,
      filter: null,
      sort: [{ column: 'name', dir: 'desc' as const }],
      scopeRowIds: null,
    };

    await expect(fetchRouteRow(apiWith(locate, read), 'sheet-1', row.id, unscoped)).resolves.toBeNull();
    expect(locate).toHaveBeenCalledOnce();
    expect(read).not.toHaveBeenCalled();
  });
});
