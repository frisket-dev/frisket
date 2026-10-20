// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import type { ComponentProps } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { DocumentList } from '../../src/workbench/DocumentList';
import { LIST_ITEM_HEIGHT } from '../../src/workbench/useWindowedRowList';

afterEach(cleanup);

function fixture() {
  const items = Array.from({ length: 100 }, (_, index) => ({
    rowId: String(index), ordinal: index, title: `Document ${index}`, titleTruncated: false,
    sourceKind: 'pdf' as const, sourceLabel: 'report.pdf', sourceLabelTruncated: false,
    sourcePresent: true, characterCount: null,
  }));
  const browse: ComponentProps<typeof DocumentList>['browse'] = {
    items, windowRows: items.slice(20, 23), startIndex: 20, search: '', setSearch: vi.fn(),
    list: { key: 'test', pages: [{ items, nextCursor: 'next', previousCursor: null }],
      pinned: null, loading: false, error: null },
    listBodyRef: { current: null }, onListScroll: vi.fn(), onListKeyDown: vi.fn(), loadMore: vi.fn(),
  };
  return { browse, activeRowId: '21', onSelect: vi.fn(), onOpen: vi.fn(),
    describe: (item: typeof items[number]) => ({ secondary: `Details ${item.rowId}` }) };
}

it('renders only the window, preserving selection, offsets and host callbacks', () => {
  const props = fixture();
  render(<DocumentList {...props} />);
  const options = screen.getAllByRole('option');
  expect(options).toHaveLength(3);
  expect(options[0]).toHaveStyle({ top: `${20 * LIST_ITEM_HEIGHT}px` });
  expect(options[0].parentElement).toHaveStyle({ height: `${100 * LIST_ITEM_HEIGHT}px` });
  expect(options[1]).toHaveAttribute('aria-selected', 'true');
  expect(options[1]).toHaveTextContent('Details 21');
  fireEvent.click(options[1]);
  fireEvent.doubleClick(options[1]);
  expect(props.onSelect).toHaveBeenCalledWith(props.browse.items[21]);
  expect(props.onOpen).toHaveBeenCalledWith('21');
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'needle' } });
  expect(props.browse.setSearch).toHaveBeenCalledWith('needle');
  fireEvent.keyDown(screen.getByRole('listbox'), { key: 'ArrowDown' });
  fireEvent.scroll(screen.getByRole('listbox'));
  expect(props.browse.onListKeyDown).toHaveBeenCalledOnce();
  expect(props.browse.onListScroll).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
  expect(props.browse.loadMore).toHaveBeenCalledOnce();
});

it('shares loading, error and empty treatments while keeping view-specific decorations', () => {
  const props = fixture();
  const { rerender } = render(<DocumentList {...props} count="Preview sample · 3 documents"
    describe={item => ({ title: <>○ {item.title}</>, secondary: 'No records' })} />);
  expect(screen.getByText('Preview sample · 3 documents')).toBeVisible();
  expect(screen.getAllByRole('option')[0]).toHaveTextContent('○ Document 20');
  props.browse.list.loading = true;
  rerender(<DocumentList {...props} />);
  expect(screen.getByRole('button', { name: 'Loading…' })).toBeDisabled();
  props.browse.items = [];
  props.browse.windowRows = [];
  props.browse.list = { ...props.browse.list, pages: [], loading: false, error: 'Could not load documents' };
  rerender(<DocumentList {...props} />);
  expect(screen.getByRole('alert')).toHaveTextContent('Could not load documents');
  expect(screen.getByText('No documents match.')).toBeVisible();
});
