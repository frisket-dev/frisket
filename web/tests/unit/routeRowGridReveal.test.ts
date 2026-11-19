// @vitest-environment jsdom
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Row } from '../../src/api/open';
import { useRouteRowFetch } from '../../src/bind/useRouteRowFetch';
import { consumeGridCellReveal, getPendingGridCellReveal } from '../../src/grid/gridCellReveal';
import { createDetailStore } from '../../src/state/detailStore';
import { createRouteStore } from '../../src/state/routeStore';
import { createSelectionStore } from '../../src/state/selectionStore';
import { columnDef, row } from '../support/domainFixtures';

const linked: Row = row({ title: 'Scale row 1500' }, { id: '1501', index: 0 });
const api = {
  locateSheetRow: vi.fn(async () => ({ found: true, rowId: '1501', index: 1500, pageOffset: 1500, pageSize: 500 })),
  getSheetData: vi.fn(async () => ({ rows: [linked], total: 1 })),
};
vi.mock('../../src/bind/useWorkspaceStores', () => ({ useWorkspaceStores: () => ({ projectApi: api }) }));

const sheet = { id: '4', columns: [columnDef({ id: '7', name: 'name' })] };

function renderRoute(drawer: Row | null, columnId?: string) {
  const route = createRouteStore('project-1');
  route.navigate({ projectId: 'project-1', sheetId: '4', actionKind: null, review: false,
    panel: { kind: 'row', rowId: '1501', ...(columnId ? { columnId } : {}) } });
  const detail = createDetailStore();
  if (drawer) detail.openRow(drawer);
  renderHook(() => useRouteRowFetch({
    route, detail, selection: createSelectionStore(), sheet,
    activeChildFilterParentRowId: null, activeGridFilter: null, activeGridSort: null, activeGridScopeRowIds: null,
    rowDrawerId: drawer?.id ?? null, rowDrawerComplete: !drawer?.loadedColumnIds, showError: vi.fn(),
  }));
  return detail;
}

beforeEach(() => {
  const pending = getPendingGridCellReveal();
  if (pending) consumeGridCellReveal(pending.requestId);
});
afterEach(cleanup);

describe('route row grid reveal', () => {
  it('asks the grid to scroll to a linked row the drawer did not show', async () => {
    const detail = renderRoute(null);
    await waitFor(() => expect(detail.store.get().rowDrawer?.id).toBe('1501'));
    expect(getPendingGridCellReveal()).toMatchObject({
      projectId: 'project-1', sheetId: '4', rowId: '1501', columnId: null, columnName: null,
    });
  });

  it('reveals a linked cell as its row only, so no cell selection turns the next click into an edit', async () => {
    const detail = renderRoute(null, '7');
    await waitFor(() => expect(detail.store.get().rowDrawer?.id).toBe('1501'));
    expect(getPendingGridCellReveal()).toMatchObject({ rowId: '1501', columnId: null, columnName: null });
  });

  it('leaves the grid alone when the drawer row came from the grid with only its projected columns', async () => {
    const detail = renderRoute({ ...linked, loadedColumnIds: ['1'] });
    await waitFor(() => expect(detail.store.get().rowDrawer?.loadedColumnIds).toBeUndefined());
    expect(getPendingGridCellReveal()).toBeNull();
  });
});
