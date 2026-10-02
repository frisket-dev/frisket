// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { DocumentListItem, DocumentListPage, Row, SheetMeta } from '../../src/api/types';
import type { DocumentViewState } from '../../src/workspace/useWorkspaceChromeState';
import { DocumentAlongsidePane } from '../../src/workbench/DocumentAlongsidePane';
import { useDocumentView, type UseDocumentViewArgs } from '../../src/workbench/useDocumentView';

vi.mock('../../src/components/RowDrawer', () => ({
  FieldValue: ({ value }: { value: unknown }) => <span>{String(value ?? '')}</span>,
  fieldValueDependencyColumnIds: (columns: Array<{ id: string; type: string; name: string }>, column: { id: string; type: string }) =>
    column.type === 'video' ? columns.filter((candidate) => candidate.id !== column.id && candidate.type === 'text')
      .map((candidate) => candidate.id) : [],
}));

afterEach(cleanup);

const sheet: SheetMeta = {
  id: '9', name: 'Documents', rowCount: 500,
  columns: [{ id: '10', name: 'file', type: 'file', ai_generated: false }],
  citedColumnIds: [], annotatedTextColumnIds: [],
};

const state: DocumentViewState = {
  sheetId: sheet.id, sourceColumnId: '10', titleColumnId: null,
  layout: 'continuous', fit: 'width', videoFit: 'full', textLayer: true,
  sync: false, activeRowId: null,
};

function item(id: number, overrides: Partial<DocumentListItem> = {}): DocumentListItem {
  return { rowId: String(id), ordinal: id, title: `Document ${id}`, titleTruncated: false,
    sourceKind: 'pdf', sourcePresent: true, sourceLabel: `${id}.pdf`,
    sourceLabelTruncated: false, characterCount: null, ...overrides };
}

function page(ids: number[], previousCursor: string | null, nextCursor: string | null): DocumentListPage {
  return { items: ids.map((id) => item(id)), previousCursor, nextCursor };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function hookArgs(overrides: Partial<UseDocumentViewArgs> = {}): UseDocumentViewArgs {
  return {
    projectId: 'project-a', sheet, state,
    onChangeState: vi.fn(), onDocumentFocus: vi.fn(),
    queryDocuments: vi.fn(async () => page([1], null, null)),
    hydrateRow: vi.fn(async () => null), orderKey: 'scope-a',
    annotatedTextColumnIds: [], ...overrides,
  };
}

describe('useDocumentView bounded paging', () => {
  it('hydrates only media and timed-transcript companion columns', async () => {
    const transcriptSheet: SheetMeta = { ...sheet, columns: [
      ...sheet.columns,
      { id: '20', name: 'transcript', type: 'timestamped_transcript', ai_generated: true },
      { id: '21', name: 'transcript_segments', type: 'json', ai_generated: true },
      { id: '22', name: 'unrelated', type: 'text', ai_generated: false },
    ] };
    const hydrateRow = vi.fn(async () => null);
    renderHook(() => useDocumentView(hookArgs({ sheet: transcriptSheet, hydrateRow })));
    await waitFor(() => expect(hydrateRow).toHaveBeenCalledWith('1', ['10', '20', '21']));
  });

  it('does not issue an empty projected row fetch for annotated text', async () => {
    const textSheet: SheetMeta = { ...sheet,
      columns: [{ id: '30', name: 'body', type: 'text', ai_generated: false }],
      annotatedTextColumnIds: ['30'] };
    const hydrateRow = vi.fn(async () => null);
    renderHook(() => useDocumentView(hookArgs({
      sheet: textSheet, state: { ...state, sourceColumnId: '30' },
      annotatedTextColumnIds: ['30'], hydrateRow,
    })));
    await waitFor(() => expect(hydrateRow).not.toHaveBeenCalled());
  });

  it('keeps at most three pages, pins an evicted active item, and navigates around it', async () => {
    const queryDocuments = vi.fn(async ({ cursor, anchorRowId }: { cursor?: string; anchorRowId?: string }) => {
      if (anchorRowId === '1') return page([1, 2], null, 'c2');
      if (!cursor) return page([1], null, 'c2');
      const number = Number(cursor.slice(1));
      return page([number], number > 2 ? `c${number - 1}` : 'c1', number < 4 ? `c${number + 1}` : null);
    });
    const onChangeState = vi.fn();
    const { result } = renderHook(() => useDocumentView(hookArgs({ queryDocuments, onChangeState })));
    await waitFor(() => expect(result.current.items.map((entry) => entry.rowId)).toEqual(['1']));

    act(() => { void result.current.loadMore(); });
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    act(() => { void result.current.loadMore(); });
    await waitFor(() => expect(result.current.items).toHaveLength(3));
    act(() => { void result.current.loadMore(); });
    await waitFor(() => expect(result.current.items.map((entry) => entry.rowId)).toEqual(['2', '3', '4']));
    expect(result.current.items.map((entry) => entry.rowId)).toEqual(['2', '3', '4']);
    expect(result.current.activeItem?.rowId).toBe('1');

    act(() => {
      result.current.onListKeyDown({ key: 'ArrowDown', preventDefault: vi.fn() } as never);
    });
    await waitFor(() => expect(onChangeState).toHaveBeenLastCalledWith(expect.objectContaining({ activeRowId: '2' })));
  });

  it('ignores an old page after the project or scope changes', async () => {
    const oldPage = deferred<DocumentListPage>();
    const oldQuery = vi.fn(() => oldPage.promise);
    const newQuery = vi.fn(async () => page([2], null, null));
    const args = hookArgs({ queryDocuments: oldQuery });
    const { result, rerender } = renderHook(
      ({ projectId, orderKey, queryDocuments }) => useDocumentView({ ...args, projectId, orderKey, queryDocuments }),
      { initialProps: { projectId: 'project-a', orderKey: 'old', queryDocuments: oldQuery } },
    );
    rerender({ projectId: 'project-b', orderKey: 'new', queryDocuments: newQuery });
    oldPage.resolve(page([1], null, null));
    await waitFor(() => expect(result.current.items.map((entry) => entry.rowId)).toEqual(['2']));
  });

  it('retries without a stale selected-row anchor after a 400', async () => {
    const anchoredError = Object.assign(new Error('outside scope'), { status: 400 });
    const queryDocuments = vi.fn(({ anchorRowId }: { anchorRowId?: string }) => anchorRowId
      ? Promise.reject(anchoredError) : Promise.resolve(page([8], null, null)));
    const { result } = renderHook(() => useDocumentView(hookArgs({
      state: { ...state, activeRowId: '99' }, queryDocuments,
    })));
    await waitFor(() => expect(result.current.items[0]?.rowId).toBe('8'));
    expect(queryDocuments).toHaveBeenNthCalledWith(1, expect.objectContaining({ anchorRowId: '99' }));
    expect(queryDocuments).toHaveBeenNthCalledWith(2, expect.objectContaining({ anchorRowId: undefined }));
  });

  it('restarts from the first page when a continuation cursor is invalid', async () => {
    const badCursor = Object.assign(new Error('stale cursor'), { status: 400 });
    const queryDocuments = vi.fn(({ cursor }: { cursor?: string }) => {
      if (cursor) return Promise.reject(badCursor);
      return Promise.resolve(queryDocuments.mock.calls.length === 1
        ? page([1], null, 'bad') : page([9], null, null));
    });
    const { result } = renderHook(() => useDocumentView(hookArgs({ queryDocuments })));
    await waitFor(() => expect(result.current.items[0]?.rowId).toBe('1'));
    act(() => { void result.current.loadMore(); });
    await waitFor(() => expect(result.current.items[0]?.rowId).toBe('9'));
    expect(result.current.list.error).toBeNull();
  });

  it('preserves the viewport anchor when a forward page evicts the first page', async () => {
    const ids = (start: number) => Array.from({ length: 100 }, (_, index) => start + index);
    const queryDocuments = vi.fn(async ({ cursor }: { cursor?: string }) => {
      const pageNumber = cursor ? Number(cursor.slice(1)) : 1;
      return page(ids((pageNumber - 1) * 100 + 1), pageNumber > 1 ? `p${pageNumber - 1}` : null,
        pageNumber < 4 ? `p${pageNumber + 1}` : null);
    });
    const { result } = renderHook(() => useDocumentView(hookArgs({ queryDocuments })));
    await waitFor(() => expect(result.current.items).toHaveLength(100));
    act(() => { void result.current.loadMore(); });
    await waitFor(() => expect(result.current.items).toHaveLength(200));
    act(() => { void result.current.loadMore(); });
    await waitFor(() => expect(result.current.items).toHaveLength(300));
    const viewport = document.createElement('div');
    Object.defineProperty(viewport, 'clientHeight', { value: 600 });
    viewport.scrollTop = 12000;
    Object.defineProperty(result.current.listBodyRef, 'current', { value: viewport, configurable: true });
    act(() => { void result.current.loadMore(); });
    await waitFor(() => expect(result.current.items[0]?.rowId).toBe('101'));
    expect(viewport.scrollTop).toBe(6000);
  });
});

describe('DocumentAlongsidePane hydration', () => {
  it('requests the selected video and its presentation companion cells', async () => {
    const columns: SheetMeta['columns'] = [
      { id: 'video', name: 'interview', type: 'video', ai_generated: false },
      { id: 'caption', name: 'interview transcript', type: 'text', ai_generated: false },
      { id: 'other', name: 'duration', type: 'number', ai_generated: false },
    ];
    const hydrateRow = vi.fn(async () => null);
    render(<DocumentAlongsidePane projectId="project-a" sheetId="9" columns={columns}
      rowId="1" hydrateRow={hydrateRow} selectedColumnId="video"
      onChangeColumn={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(hydrateRow).toHaveBeenCalledWith('1', ['video', 'caption']));
  });

  it('does not flash the previous row while the next value is loading', async () => {
    const second = deferred<Row | null>();
    const hydrateRow = vi.fn((rowId: string) => rowId === '1'
      ? Promise.resolve({ id: '1', index: 0, cells: { '10': 'first value' }, provenance: {} })
      : second.promise);
    const props = { projectId: 'project-a', sheetId: sheet.id, columns: sheet.columns,
      hydrateRow, selectedColumnId: '10', onChangeColumn: vi.fn(), onClose: vi.fn() };
    const { rerender } = render(<DocumentAlongsidePane {...props} rowId="1" />);
    await screen.findByText('first value');
    rerender(<DocumentAlongsidePane {...props} rowId="2" />);
    expect(screen.queryByText('first value')).toBeNull();
    expect(screen.getByText('Loading value…')).toBeVisible();
    second.resolve({ id: '2', index: 1, cells: { '10': 'second value' }, provenance: {} });
    await screen.findByText('second value');
  });
});
