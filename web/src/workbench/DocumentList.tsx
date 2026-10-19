import type { ReactNode } from 'react';
import { Button } from '../components/Button';
import { SearchInput } from '../components/FormControls';
import type { useDocumentView } from './useDocumentView';
import { LIST_ITEM_HEIGHT } from './useWindowedRowList';

type Browse = Pick<ReturnType<typeof useDocumentView>, 'search' | 'setSearch' | 'list' | 'items' |
  'listBodyRef' | 'onListScroll' | 'onListKeyDown' | 'windowRows' | 'startIndex' | 'loadMore'>;
type Item = Browse['windowRows'][number];

/** Presentation for the existing windowed document browser; no fetching or selection state. */
export function DocumentList({ browse, activeRowId, count, onSelect, onOpen, describe }: {
  browse: Browse;
  activeRowId: string | null;
  count?: ReactNode;
  onSelect(item: Item): void;
  onOpen?(rowId: string): void;
  describe(item: Item): { title?: ReactNode; secondary: ReactNode };
}) {
  const { search, setSearch, list, items, listBodyRef, onListScroll, onListKeyDown,
    windowRows, startIndex, loadMore } = browse;
  return <aside className="document-list" data-testid="document-list" aria-label="Documents">
    <SearchInput data-testid="document-list-search" placeholder="Search documents…" aria-label="Search documents"
      value={search} onChange={event => setSearch(event.target.value)} />
    <div className="document-list-count muted mono">{count ?? `${items.length.toLocaleString()} loaded`}</div>
    <div className="document-list-body drawer-body" data-testid="document-list-body" ref={listBodyRef}
      onScroll={onListScroll} onKeyDown={onListKeyDown} tabIndex={0} role="listbox" aria-label="Document list">
      {list.error && <div className="document-list-message" role="alert">{list.error}</div>}
      {!list.loading && items.length === 0 && <div className="document-list-empty" data-testid="document-list-empty">No documents match.</div>}
      <div className="document-list-scroller" style={{ height: items.length * LIST_ITEM_HEIGHT }}>
        {windowRows.map((item, offset) => {
          const active = item.rowId === activeRowId;
          const content = describe(item);
          return <button type="button" key={item.rowId} className={`document-list-item${active ? ' active' : ''}`}
            data-testid="document-list-item" data-row-id={item.rowId} data-active={active}
            data-has-media={item.sourceKind !== 'annotated_text' && item.sourcePresent}
            role="option" aria-selected={active}
            style={{ position: 'absolute', top: (startIndex + offset) * LIST_ITEM_HEIGHT, height: LIST_ITEM_HEIGHT }}
            onClick={() => onSelect(item)} onDoubleClick={onOpen ? () => onOpen(item.rowId) : undefined}>
            <span className="document-list-item-title">{content.title ?? item.title}</span>
            <span className="document-list-item-secondary muted">{content.secondary}</span>
          </button>;
        })}
      </div>
      {list.pages.at(-1)?.nextCursor && <Button type="button" className="document-list-more" data-testid="document-list-more"
        disabled={list.loading} onClick={() => void loadMore()}>{list.loading ? 'Loading…' : 'Load more'}</Button>}
    </div>
  </aside>;
}
