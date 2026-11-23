// @vitest-environment jsdom
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ColumnValuesPreview } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { createWorkspaceStores } from '../../src/state/createWorkspaceStores';
import { useFacetEligibility } from '../../src/workspace/useFacetEligibility';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';

const api = createProjectApi('facet-cap');
const stores = createWorkspaceStores('facet-cap', api);
const wrapper = ({ children }: { children: ReactNode }) => (
  <WorkspaceStoresContext.Provider value={stores}>{children}</WorkspaceStoresContext.Provider>
);
const sheet = sheetMeta([
  columnDef({ id: '1', name: 'kind', type: 'text' }),
  columnDef({ id: '2', name: 'body', type: 'text' }),
  columnDef({ id: '3', name: 'ocr', type: 'text' }),
  columnDef({ id: '4', name: 'size', type: 'number' }),
], { id: '9', name: 'Filings', rowCount: 400 });

function preview(inputColumn: string, distinct: number, omittedRows = 0): ColumnValuesPreview {
  return {
    sheetId: '9', columnId: '1', inputColumn, totalRows: 400, distinct, missing: 0, values: [],
    offset: 0, limit: 1, truncated: distinct > 0, valueHash: null, search: null, distribution: null,
    inventoryComplete: omittedRows === 0, omittedRows,
  };
}

afterEach(() => vi.restoreAllMocks());

it('asks the server for each column’s whole-column distinct count and leaves large ones to search', async () => {
  const calls = vi.spyOn(api, 'columnValuesPreview').mockImplementation(async (input) => (
    input.inputColumn === 'kind' ? preview('kind', 4)
      : input.inputColumn === 'body' ? preview('body', 51)
        : preview('ocr', 2, 6)
  ));
  const { result } = renderHook(() => useFacetEligibility(sheet, 0), { wrapper });
  expect(result.current.pending.has('kind')).toBe(true);
  await waitFor(() => expect(result.current.pending.size).toBe(0));
  expect([...result.current.searchOnly].sort()).toEqual(['body', 'ocr']);
  expect(calls.mock.calls.map(([input]) => input.inputColumn).sort()).toEqual(['body', 'kind', 'ocr']);
  expect(calls).toHaveBeenCalledWith(expect.objectContaining({ sheetId: '9', valueMode: 'bounded', limit: 1 }));
});
