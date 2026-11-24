// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
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

afterEach(() => vi.restoreAllMocks());

it('costs nothing until asked, then counts every column in one server request', async () => {
  const counts = vi.spyOn(api, 'columnValueCounts').mockResolvedValue({
    columns: [
      { inputColumn: 'kind', distinct: 4, omittedRows: 0, listDistinct: null },
      { inputColumn: 'body', distinct: 51, omittedRows: 0, listDistinct: null },
      { inputColumn: 'ocr', distinct: 2, omittedRows: 6, listDistinct: null },
    ],
  });
  const preview = vi.spyOn(api, 'columnValuesPreview');
  const { result } = renderHook(() => useFacetEligibility(sheet, 0), { wrapper });
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(counts).not.toHaveBeenCalled();
  expect(result.current.pending.size).toBe(3);
  act(() => result.current.ensure());
  act(() => result.current.ensure());
  await waitFor(() => expect(result.current.pending.size).toBe(0));
  expect(counts).toHaveBeenCalledTimes(1);
  expect(counts).toHaveBeenCalledWith({ sheetId: '9', inputColumns: ['kind', 'body', 'ocr'] });
  expect(preview).not.toHaveBeenCalled();
  expect([...result.current.searchOnly].sort()).toEqual(['body', 'ocr']);
});

it('keeps the current counts when older counts finish last', async () => {
  type Counts = Awaited<ReturnType<typeof api.columnValueCounts>>;
  let oldDone!: (value: Counts) => void;
  let newDone!: (value: Counts) => void;
  vi.spyOn(api, 'columnValueCounts')
    .mockImplementationOnce(() => new Promise((resolve) => { oldDone = resolve; }))
    .mockImplementationOnce(() => new Promise((resolve) => { newDone = resolve; }));
  const { result, rerender } = renderHook(({ version }) => useFacetEligibility(sheet, version), {
    wrapper, initialProps: { version: 0 },
  });
  act(() => result.current.ensure());
  rerender({ version: 1 });
  act(() => result.current.ensure());
  const counts: Counts = {
    columns: ['kind', 'body', 'ocr'].map((inputColumn) => ({
      inputColumn, distinct: 3, omittedRows: 0, listDistinct: null,
    })),
  };
  await act(async () => { newDone(counts); });
  expect(result.current.pending.size).toBe(0);
  await act(async () => { oldDone(counts); });
  act(() => result.current.ensure());
  expect(result.current.pending.size).toBe(0);
});
