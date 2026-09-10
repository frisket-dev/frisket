// The shared frequency-sorted column-value list for the four "resolve" actions.
// Presentational + fully controlled: the parent owns `selected`; this
// component owns only view state (search query, keyboard-active row,
// scroll window). Windowing follows workbench/useWindowedRowList.ts
// (scrollTop + ResizeObserver measure, fixed row height, ±OVERSCAN), but is
// local because the selection semantics differ: rows here are multi-select
// checkboxes (click toggles, shift-click ranges, ⌘/ctrl-click toggles
// without clearing — spec 5d.1), not a single active-row cursor.
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from 'react';
import { Search } from 'lucide-react';
import './resolve.css';

export interface ValueListEntry {
  value: string;
  count: number;
}

const OVERSCAN = 6;
const DEFAULT_ROW_HEIGHT = 28;
/** Fallback viewport height until the ResizeObserver measures a real one
 *  (and forever under jsdom, which has neither RO nor layout). */
const FALLBACK_VIEWPORT_HEIGHT = 360;

/** Render a value with its leading/trailing whitespace made visible —
 *  matching is exact, so "Acme " and "Acme" are different values and the
 *  padded region gets a subtle dotted-underline span (white-space: pre keeps
 *  the actual characters). Module-private helper, not a component. */
function renderValueLabel(value: string): ReactNode {
  const lead = /^\s+/.exec(value)?.[0] ?? '';
  const rest = value.slice(lead.length);
  const trail = rest.length > 0 ? /\s+$/.exec(rest)?.[0] ?? '' : '';
  const core = rest.slice(0, rest.length - trail.length);
  if (lead === '' && trail === '') return value;
  return (
    <>
      {lead !== '' && (
        <span
          className="resolve-value-ws"
          data-testid="resolve-value-ws"
          data-ws="leading"
          title="Leading whitespace — matching is exact"
        >
          {lead}
        </span>
      )}
      {core}
      {trail !== '' && (
        <span
          className="resolve-value-ws"
          data-testid="resolve-value-ws"
          data-ws="trailing"
          title="Trailing whitespace — matching is exact"
        >
          {trail}
        </span>
      )}
    </>
  );
}

export function ValueList({
  values,
  selected,
  onSelectionChange,
  renderTrailing,
  searchable = false,
  searchPlaceholder = 'Filter values',
  showCountBar = true,
  rowHeight = DEFAULT_ROW_HEIGHT,
  ariaLabel = 'Column values',
  emptyText = 'No values',
  testId = 'resolve-value-list',
}: {
  /** Distinct values with occurrence counts; rendered sorted by count
   *  descending (ties keep input order). */
  values: readonly ValueListEntry[];
  /** Controlled selection, keyed by value string. */
  selected: ReadonlySet<string>;
  /** Called with a NEW Set on every selection gesture. */
  onSelectionChange(next: Set<string>): void;
  /** Per-row trailing affordances (e.g. an "add to group" button). Clicks
   *  inside the trailing slot do NOT toggle the row's selection. */
  renderTrailing?(entry: ValueListEntry): ReactNode;
  /** Shows the built-in ⌕ filter input. Filtering never mutates selection —
   *  hidden rows stay selected. */
  searchable?: boolean;
  searchPlaceholder?: string;
  /** Per-row proportional frequency bar (relative to the max count). */
  showCountBar?: boolean;
  /** Fixed row height in px — drives the windowing math. */
  rowHeight?: number;
  ariaLabel?: string;
  emptyText?: string;
  testId?: string;
}) {
  const listId = useId();
  const [query, setQuery] = useState('');
  /** Keyboard-active row (roving highlight), by value. */
  const [activeValue, setActiveValue] = useState<string | null>(null);
  /** Shift-click range anchor: the last plainly-clicked row's value. */
  const anchorRef = useRef<string | null>(null);

  const sorted = useMemo(
    () => [...values].sort((a, b) => b.count - a.count),
    [values],
  );
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sorted;
    return sorted.filter((entry) => entry.value.toLowerCase().includes(q));
  }, [sorted, query]);
  const maxCount = sorted.length > 0 ? sorted[0].count : 0;

  // ── windowing (useWindowedRowList pattern, local) ──────────────────────
  const viewportRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(FALLBACK_VIEWPORT_HEIGHT);

  useEffect(() => {
    const node = viewportRef.current;
    if (!node || typeof ResizeObserver === 'undefined') return;
    const measure = () => {
      if (node.clientHeight > 0) setViewportHeight(node.clientHeight);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const startIndex = Math.max(0, Math.floor(scrollTop / rowHeight) - OVERSCAN);
  const endIndex = Math.min(
    visible.length,
    Math.ceil((scrollTop + viewportHeight) / rowHeight) + OVERSCAN,
  );
  const windowRows = visible.slice(startIndex, endIndex);

  const rowDomId = (index: number) => `${listId}-row-${index}`;
  const activeIndex = activeValue === null
    ? -1
    : visible.findIndex((entry) => entry.value === activeValue);

  // ── selection gestures ─────────────────────────────────────────────────
  const handleRowClick = (
    entry: ValueListEntry,
    index: number,
    event: MouseEvent<HTMLDivElement>,
  ) => {
    setActiveValue(entry.value);
    if (event.shiftKey && anchorRef.current !== null) {
      const anchorIndex = visible.findIndex(
        (candidate) => candidate.value === anchorRef.current,
      );
      if (anchorIndex !== -1) {
        // Range-select anchor→row (inclusive), unioned with the selection.
        const next = new Set(selected);
        const [lo, hi] = anchorIndex < index ? [anchorIndex, index] : [index, anchorIndex];
        for (let i = lo; i <= hi; i += 1) next.add(visible[i].value);
        onSelectionChange(next);
        return;
      }
    }
    // Plain and ⌘/ctrl click both toggle membership without clearing the
    // rest — the checkbox model is "click rows to select … then group".
    const next = new Set(selected);
    if (next.has(entry.value)) next.delete(entry.value);
    else next.add(entry.value);
    anchorRef.current = entry.value;
    onSelectionChange(next);
  };

  const scrollIndexIntoView = (index: number) => {
    const node = viewportRef.current;
    if (!node) return;
    const top = index * rowHeight;
    if (top < node.scrollTop) {
      node.scrollTop = top;
      setScrollTop(top);
    } else if (node.clientHeight > 0 && top + rowHeight > node.scrollTop + node.clientHeight) {
      const nextTop = top + rowHeight - node.clientHeight;
      node.scrollTop = nextTop;
      setScrollTop(nextTop);
    }
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (visible.length === 0) return;
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      const nextIndex = activeIndex === -1
        ? (delta === 1 ? 0 : visible.length - 1)
        : Math.min(Math.max(activeIndex + delta, 0), visible.length - 1);
      const nextRow = visible[nextIndex];
      setActiveValue(nextRow.value);
      scrollIndexIntoView(nextIndex);
      if (event.shiftKey) {
        // Shift+Arrow grows the selection as it moves, like shift-click.
        const next = new Set(selected);
        next.add(nextRow.value);
        onSelectionChange(next);
      }
      return;
    }
    if ((event.key === 'Enter' || event.key === ' ') && activeValue !== null) {
      event.preventDefault();
      const next = new Set(selected);
      if (next.has(activeValue)) next.delete(activeValue);
      else next.add(activeValue);
      anchorRef.current = activeValue;
      onSelectionChange(next);
    }
  };

  return (
    <div className="resolve-value-list" data-testid={testId}>
      {searchable && (
        <div className="resolve-value-search-row">
          <Search size={13} aria-hidden />
          <input
            className="form-input resolve-value-search"
            data-testid="resolve-value-search"
            value={query}
            placeholder={searchPlaceholder}
            aria-label="Filter values"
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      )}
      {visible.length === 0 ? (
        <div className="panel-empty resolve-value-empty" data-testid="resolve-value-empty">
          {query.trim() ? `No values match "${query.trim()}"` : emptyText}
        </div>
      ) : (
        <div
          ref={viewportRef}
          className="resolve-value-viewport"
          role="listbox"
          aria-multiselectable="true"
          aria-label={ariaLabel}
          aria-activedescendant={activeIndex === -1 ? undefined : rowDomId(activeIndex)}
          tabIndex={0}
          onScroll={() => {
            if (viewportRef.current) setScrollTop(viewportRef.current.scrollTop);
          }}
          onKeyDown={handleKeyDown}
        >
          <div
            className="resolve-value-spacer"
            style={{ height: visible.length * rowHeight }}
          >
            {windowRows.map((entry, offset) => {
              const index = startIndex + offset;
              const isSelected = selected.has(entry.value);
              const classes = [
                'resolve-value-row',
                isSelected && 'is-selected',
                entry.value === activeValue && 'is-active',
              ]
                .filter(Boolean)
                .join(' ');
              return (
                <div
                  key={entry.value}
                  id={rowDomId(index)}
                  role="option"
                  aria-selected={isSelected}
                  className={classes}
                  data-testid="resolve-value-row"
                  data-value={entry.value}
                  style={{ top: index * rowHeight, height: rowHeight }}
                  onClick={(event) => handleRowClick(entry, index, event)}
                >
                  {showCountBar && maxCount > 0 && (
                    <span
                      className="resolve-value-bar"
                      style={{ width: `${(entry.count / maxCount) * 100}%` }}
                      aria-hidden
                    />
                  )}
                  <span className="resolve-value-text" title={entry.value}>
                    {renderValueLabel(entry.value)}
                  </span>
                  <span className="resolve-value-count">{entry.count.toLocaleString()}</span>
                  {renderTrailing && (
                    <span
                      className="resolve-value-trailing"
                      onClick={(event) => event.stopPropagation()}
                    >
                      {renderTrailing(entry)}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
