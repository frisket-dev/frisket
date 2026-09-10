// Shared row-list windowing for the AnswersView ↔ DocumentView port pair: both
// re-implemented the identical onListScroll + ResizeObserver measure block, the
// same window math (floor/ceil ±6 overscan), and the same ArrowUp/Down active-row
// nav. This hook is that shared mechanics; bespoke row JSX and what "select" does
// (sync to the shared grid selection vs set a local browse cursor) stay at each
// call site as params/JSX.
//
// `Row.id` (api/types.ts) is typed `string`, so this hook compares `row.id`
// directly rather than wrapping it in `String(...)` — the wrap would be a no-op.
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type RefObject } from 'react';

/** Fixed row height both views render at — drives the scroller's total
 *  height and the window math alike. */
export const LIST_ITEM_HEIGHT = 60;

const OVERSCAN = 6;

function scrollIndexIntoView(node: HTMLDivElement, index: number): void {
  const rowTop = index * LIST_ITEM_HEIGHT;
  const rowBottom = rowTop + LIST_ITEM_HEIGHT;
  const viewportTop = node.scrollTop;
  const viewportBottom = viewportTop + node.clientHeight;

  if (rowTop < viewportTop) {
    node.scrollTop = rowTop;
  } else if (rowBottom > viewportBottom) {
    node.scrollTop = rowBottom - node.clientHeight;
  }
}

export interface UseWindowedRowListArgs<T extends { id: string }> {
  /** The full (already search-filtered) row list to window over. */
  rows: readonly T[];
  /** The currently active row id — ArrowUp/ArrowDown nav starts from its
   *  index in `rows`. */
  activeId: string | null;
  /** Called with the next row's id when Arrow nav moves the active row
   *  (AnswersView: onSyncSelectRow; DocumentView: selectDocument). */
  onSelect(id: string): void;
}

export interface WindowedRowList<T> {
  listBodyRef: RefObject<HTMLDivElement>;
  onListScroll(): void;
  onListKeyDown(event: KeyboardEvent<HTMLDivElement>): void;
  /** Index of `windowRows[0]` within `rows` — callers use it to position
   *  each rendered row (`top: (startIndex + offset) * LIST_ITEM_HEIGHT`). */
  startIndex: number;
  windowRows: T[];
}

/** Scroll position → windowed slice, a ResizeObserver height measure, and
 *  ArrowUp/ArrowDown active-row nav. */
export function useWindowedRowList<T extends { id: string }>({
  rows,
  activeId,
  onSelect,
}: UseWindowedRowListArgs<T>): WindowedRowList<T> {
  const listBodyRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [listHeight, setListHeight] = useState(600);

  const onListScroll = useCallback(() => {
    if (listBodyRef.current) setScrollTop(listBodyRef.current.scrollTop);
  }, []);

  useEffect(() => {
    const node = listBodyRef.current;
    if (!node) return;
    const measure = () => setListHeight(node.clientHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const startIndex = Math.max(0, Math.floor(scrollTop / LIST_ITEM_HEIGHT) - OVERSCAN);
  const endIndex = Math.min(
    rows.length,
    Math.ceil((scrollTop + listHeight) / LIST_ITEM_HEIGHT) + OVERSCAN,
  );
  const windowRows = rows.slice(startIndex, endIndex) as T[];

  const onListKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      event.preventDefault();
      const index = rows.findIndex((row) => row.id === activeId);
      const nextIndex = Math.min(
        Math.max((index === -1 ? 0 : index) + (event.key === 'ArrowDown' ? 1 : -1), 0),
        rows.length - 1,
      );
      const nextRow = rows[nextIndex];
      if (nextRow) {
        const listBody = listBodyRef.current;
        if (listBody) scrollIndexIntoView(listBody, nextIndex);
        onSelect(nextRow.id);
      }
    },
    [rows, activeId, onSelect],
  );

  return { listBodyRef, onListScroll, onListKeyDown, startIndex, windowRows };
}
