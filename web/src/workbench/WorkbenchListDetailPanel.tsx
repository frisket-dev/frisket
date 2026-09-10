import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react';

// A generic "list of records on the left, detail of the selected record on the
// right" panel for the bottom dock. Jobs/Errors and History are both expressed
// as thin adapters over this: declare the list `columns` and a `renderDetail`,
// and the split-view machinery (resizable + persisted column widths, resizable
// + persisted detail pane, selection defaulting, scroll-the-active-row-into-view)
// is shared. Escape hatches — `rowClassName`, `listHeader`/`listFooter`, and a
// free-form `renderDetail` — cover the per-host differences.

const DETAIL_WIDTH_DEFAULT = 360;
const DETAIL_WIDTH_MIN = 260;
const DETAIL_WIDTH_MAX = 720;
const COLUMN_WIDTH_MIN = 72;
const COLUMN_WIDTH_MAX = 640;
const COLUMN_WIDTH_FALLBACK = 120;

export interface ListDetailColumn<T> {
  key: string;
  label: string;
  defaultWidth?: number;
  render(item: T): ReactNode;
  cellClassName?(item: T): string | undefined;
}

export interface WorkbenchListDetailPanelProps<T> {
  items: T[];
  getRowId(item: T): string;
  columns: ListDetailColumn<T>[];
  renderDetail(item: T): ReactNode;
  /** Fully-resolved message shown in the empty table body (loading/error/empty). */
  emptyText: ReactNode;
  ariaLabel: string;
  detailAriaLabel?: string;
  detailEmptyText?: ReactNode;
  /** localStorage key for the per-column width map. */
  tableWidthsStorageKey: string;
  /** localStorage key for the detail pane width (shared across dock panels). */
  detailWidthStorageKey: string;
  /** Extra classes on a row (e.g. history applied/current/undone state). */
  rowClassName?(item: T): string | undefined;
  /** Extra data-* attributes on a row (kept as-is for existing testids). */
  rowData?(item: T): Record<string, string>;
  rowTestId?(item: T): string;
  /** Which row is selected by default when nothing is picked (id, or null → first row). */
  defaultSelectedId?(items: T[]): string | null;
  /** Row that should stay scrolled into view + marked active (e.g. history cursor). */
  activeRowId?: string | null;
  columnHeaderTestId?(key: string): string;
  columnResizeTestId?(key: string): string;
  splitResizeTestId?: string;
  listTestId?: string;
  /** Rendered above the table (e.g. "Older" pagination). */
  listHeader?: ReactNode;
  /** Rendered below the table (e.g. "Newer" pagination). */
  listFooter?: ReactNode;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function loadDetailWidth(storageKey: string): number {
  const stored = Number(localStorage.getItem(storageKey));
  if (!Number.isFinite(stored) || stored <= 0) return DETAIL_WIDTH_DEFAULT;
  return clamp(stored, DETAIL_WIDTH_MIN, DETAIL_WIDTH_MAX);
}

function loadColumnWidths(storageKey: string): Record<string, number> {
  try {
    const stored = localStorage.getItem(storageKey);
    if (!stored) return {};
    const parsed = JSON.parse(stored) as Record<string, unknown>;
    return Object.fromEntries(
      Object.entries(parsed).flatMap(([key, value]) => {
        if (typeof value !== 'number' || !Number.isFinite(value)) return [];
        return [[key, clamp(value, COLUMN_WIDTH_MIN, COLUMN_WIDTH_MAX)]];
      }),
    );
  } catch {
    return {};
  }
}

/** Shared detail-pane header: bold title + optional status chip. */
export function ListDetailHeader({
  title,
  status,
  statusClassName,
}: {
  title: ReactNode;
  status?: ReactNode;
  statusClassName?: string;
}) {
  return (
    <div className="bottom-dock-detail-header">
      <strong>{title}</strong>
      {status !== undefined && <span className={statusClassName}>{status}</span>}
    </div>
  );
}

/** Shared label/value grid used by detail bodies. */
export function ListDetailGrid({ rows }: { rows: Array<[ReactNode, ReactNode]> }) {
  return (
    <dl className="bottom-dock-detail-grid">
      {rows.map(([label, value]) => (
        <div key={String(label)}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function WorkbenchListDetailPanel<T>({
  items,
  getRowId,
  columns,
  renderDetail,
  emptyText,
  ariaLabel,
  detailAriaLabel,
  detailEmptyText = 'Select a row to inspect it.',
  tableWidthsStorageKey,
  detailWidthStorageKey,
  rowClassName,
  rowData,
  rowTestId,
  defaultSelectedId,
  activeRowId = null,
  columnHeaderTestId,
  columnResizeTestId,
  splitResizeTestId,
  listTestId,
  listHeader,
  listFooter,
}: WorkbenchListDetailPanelProps<T>) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedItem = useMemo(() => {
    if (items.length === 0) return null;
    const picked = items.find((item) => getRowId(item) === selectedId);
    if (picked) return picked;
    const defaultId = defaultSelectedId ? defaultSelectedId(items) : null;
    if (defaultId != null) {
      const byDefault = items.find((item) => getRowId(item) === defaultId);
      if (byDefault) return byDefault;
    }
    return items[0];
  }, [items, selectedId, getRowId, defaultSelectedId]);
  const selectedRowId = selectedItem ? getRowId(selectedItem) : null;

  const [columnWidths, setColumnWidths] = useState<Record<string, number>>(() =>
    loadColumnWidths(tableWidthsStorageKey),
  );
  const [detailWidth, setDetailWidth] = useState(() => loadDetailWidth(detailWidthStorageKey));
  const [resizingDetail, setResizingDetail] = useState(false);

  const columnWidthFor = useCallback(
    (column: ListDetailColumn<T>) =>
      columnWidths[column.key] ?? column.defaultWidth ?? COLUMN_WIDTH_FALLBACK,
    [columnWidths],
  );
  const tableWidth = columns.reduce((total, column) => total + columnWidthFor(column), 0);

  const setPersistedColumnWidth = useCallback(
    (key: string, nextWidth: number) => {
      const clamped = clamp(nextWidth, COLUMN_WIDTH_MIN, COLUMN_WIDTH_MAX);
      setColumnWidths((current) => {
        const next = { ...current, [key]: clamped };
        localStorage.setItem(tableWidthsStorageKey, JSON.stringify(next));
        return next;
      });
    },
    [tableWidthsStorageKey],
  );
  const onColumnResizeStart = useCallback(
    (column: ListDetailColumn<T>, event: ReactPointerEvent<HTMLButtonElement>) => {
      event.preventDefault();
      event.stopPropagation();
      const startX = event.clientX;
      const startWidth = columnWidthFor(column);
      let nextWidth = startWidth;
      const onMove = (moveEvent: PointerEvent) => {
        nextWidth = clamp(startWidth + moveEvent.clientX - startX, COLUMN_WIDTH_MIN, COLUMN_WIDTH_MAX);
        setColumnWidths((current) => ({ ...current, [column.key]: nextWidth }));
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        setPersistedColumnWidth(column.key, nextWidth);
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    },
    [columnWidthFor, setPersistedColumnWidth],
  );
  const onColumnResizeKeyDown = useCallback(
    (column: ListDetailColumn<T>, event: ReactKeyboardEvent<HTMLButtonElement>) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      event.stopPropagation();
      const step = event.shiftKey ? 48 : 24;
      setPersistedColumnWidth(column.key, columnWidthFor(column) + (event.key === 'ArrowRight' ? step : -step));
    },
    [columnWidthFor, setPersistedColumnWidth],
  );

  const setPersistedDetailWidth = useCallback(
    (nextWidth: number) => {
      const clamped = clamp(nextWidth, DETAIL_WIDTH_MIN, DETAIL_WIDTH_MAX);
      setDetailWidth(clamped);
      localStorage.setItem(detailWidthStorageKey, String(clamped));
    },
    [detailWidthStorageKey],
  );
  const onDetailResizeStart = useCallback(
    (event: ReactPointerEvent<HTMLButtonElement>) => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = detailWidth;
      let nextWidth = startWidth;
      setResizingDetail(true);
      const onMove = (moveEvent: PointerEvent) => {
        nextWidth = clamp(startWidth + startX - moveEvent.clientX, DETAIL_WIDTH_MIN, DETAIL_WIDTH_MAX);
        setDetailWidth(nextWidth);
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        setResizingDetail(false);
        setPersistedDetailWidth(nextWidth);
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    },
    [detailWidth, setPersistedDetailWidth],
  );
  const onDetailResizeKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLButtonElement>) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const step = event.shiftKey ? 48 : 24;
      setPersistedDetailWidth(detailWidth + (event.key === 'ArrowLeft' ? step : -step));
    },
    [detailWidth, setPersistedDetailWidth],
  );

  // Keep the active row (e.g. the history cursor) in view as items land.
  const activeRowRef = useRef<HTMLTableRowElement | null>(null);
  useEffect(() => {
    activeRowRef.current?.scrollIntoView({ block: 'nearest' });
  }, [activeRowId]);

  return (
    <div className={`bottom-dock-split${resizingDetail ? ' bottom-dock-split-resizing' : ''}`}>
      <div className="bottom-dock-table-wrap">
        {listHeader}
        <table
          className="bottom-dock-table"
          style={{ width: tableWidth, minWidth: '100%' }}
          aria-label={ariaLabel}
          data-testid={listTestId}
        >
          <colgroup>
            {columns.map((column) => (
              <col key={column.key} style={{ width: columnWidthFor(column) }} />
            ))}
          </colgroup>
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column.key} data-testid={columnHeaderTestId?.(column.key)}>
                  <div className="bottom-dock-column-header">
                    <span>{column.label}</span>
                    <button
                      type="button"
                      className="bottom-dock-column-resize-handle"
                      data-testid={columnResizeTestId?.(column.key)}
                      aria-label={`Resize ${column.label} column`}
                      onPointerDown={(event) => onColumnResizeStart(column, event)}
                      onKeyDown={(event) => onColumnResizeKeyDown(column, event)}
                    />
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const id = getRowId(item);
              const isSelected = id === selectedRowId;
              const isActive = activeRowId != null && id === activeRowId;
              const rowClasses = [rowClassName?.(item), isSelected ? 'bottom-dock-row-selected' : null]
                .filter(Boolean)
                .join(' ');
              return (
                <tr
                  key={id}
                  ref={isActive ? activeRowRef : undefined}
                  className={rowClasses || undefined}
                  data-testid={rowTestId?.(item)}
                  {...(rowData?.(item) ?? {})}
                  onClick={() => setSelectedId(id)}
                >
                  {columns.map((column, columnIndex) => (
                    <td key={column.key} className={column.cellClassName?.(item)}>
                      {columnIndex === 0 ? (
                        <button
                          type="button"
                          className="bottom-dock-row-button"
                          aria-pressed={isSelected}
                          onClick={(event) => {
                            event.stopPropagation();
                            setSelectedId(id);
                          }}
                        >
                          {column.render(item)}
                        </button>
                      ) : (
                        column.render(item)
                      )}
                    </td>
                  ))}
                </tr>
              );
            })}
            {items.length === 0 && (
              <tr>
                <td colSpan={columns.length} className="bottom-dock-empty-cell">
                  {emptyText}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        {listFooter}
      </div>
      <button
        type="button"
        className="bottom-dock-split-handle"
        data-testid={splitResizeTestId}
        aria-label="Resize selected row details"
        onPointerDown={onDetailResizeStart}
        onKeyDown={onDetailResizeKeyDown}
      />
      <aside
        className={`bottom-dock-detail${selectedItem ? '' : ' bottom-dock-detail-empty'}`}
        style={{ flexBasis: detailWidth }}
        aria-label={detailAriaLabel}
      >
        {selectedItem ? renderDetail(selectedItem) : detailEmptyText}
      </aside>
    </div>
  );
}
