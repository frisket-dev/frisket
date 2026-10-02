import { Check, Columns2, Copy, Maximize2, X } from 'lucide-react';
import { useEffect, useId, useRef, useState, type CSSProperties } from 'react';
import type { ColumnDef, Row } from '../api/types';
import { FieldValue, fieldValueDependencyColumnIds } from '../components/RowDrawer';
import { PanelSelect } from '../components/PanelSelect';
import { ResizeSeam } from '../components/ResizeSeam';
import { useResizable } from '../components/useResizable';
import styles from './DocumentAlongsidePane.module.css';

interface DocumentAlongsidePaneProps {
  projectId: string;
  sheetId: string;
  columns: ColumnDef[];
  rowId: string | null;
  hydrateRow(rowId: string, columnIds: string[]): Promise<Row | null>;
  selectedColumnId: string | null;
  onChangeColumn(columnId: string | null): void;
  onClose(): void;
}

const MIN_WIDTH = 240;
const MAX_WIDTH = 640;

/** A second reading surface for the current row; rendering stays with FieldValue. */
export function DocumentAlongsidePane({
  projectId, sheetId, columns, rowId, hydrateRow, selectedColumnId, onChangeColumn, onClose,
}: DocumentAlongsidePaneProps) {
  const selectedColumn = columns.find((column) => column.id === selectedColumnId) ?? null;
  const hydrateRowRef = useRef(hydrateRow);
  useEffect(() => { hydrateRowRef.current = hydrateRow; }, [hydrateRow]);
  const [choosing, setChoosing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [copyStatus, setCopyStatus] = useState<{
    rowId: string | undefined; columnId: string | null; value: Row['cells'][string]; text: string;
  } | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const pickerRef = useRef<HTMLSelectElement>(null);
  const changeRef = useRef<HTMLButtonElement>(null);
  const chooserVisible = choosing || !selectedColumn;
  const hydrationKey = `${projectId}:${sheetId}:${rowId ?? ''}:${selectedColumn?.id ?? ''}`;
  const [hydrated, setHydrated] = useState<{ key: string; row: Row | null; loading: boolean; error: string | null }>({
    key: '', row: null, loading: false, error: null,
  });
  useEffect(() => {
    if (!rowId || !selectedColumn) {
      return;
    }
    let cancelled = false;
    const columnIds = [selectedColumn.id, ...fieldValueDependencyColumnIds(columns, selectedColumn)];
    void hydrateRowRef.current(rowId, columnIds).then((row) => {
      if (!cancelled) setHydrated({ key: hydrationKey, row, loading: false, error: null });
    }).catch((error: unknown) => {
      if (!cancelled) setHydrated({ key: hydrationKey, row: null, loading: false,
        error: error instanceof Error ? error.message : 'Could not load value.' });
    });
    return () => { cancelled = true; };
  }, [columns, hydrationKey, rowId, selectedColumn]);
  const row = hydrated.key === hydrationKey ? hydrated.row : null;
  useEffect(() => {
    if (chooserVisible) pickerRef.current?.focus();
    else changeRef.current?.focus();
  }, [chooserVisible]);
  const headingId = useId();
  const expandedHeadingId = useId();
  const { width, onResizeStart, onResizeKeyDown } = useResizable({
    storageKey: `frisket:document-alongside-width:${projectId}:${sheetId}`,
    minWidth: MIN_WIDTH, maxWidth: MAX_WIDTH, defaultWidth: 340, handleEdge: 'left',
  });
  const value = selectedColumn && row ? row.cells[selectedColumn.id] ?? null : null;
  const status = copyStatus?.rowId === row?.id && copyStatus?.columnId === selectedColumnId
    && copyStatus?.value === value ? copyStatus.text : '';

  useEffect(() => {
    const element = dialog.current;
    if (!expanded || !element) return;
    element.showModal();
    return () => { element.close(); };
  }, [expanded]);

  async function copyValue() {
    try {
      await navigator.clipboard.writeText(typeof value === 'string' ? value : JSON.stringify(value, null, 2));
      setCopyStatus({ rowId: row?.id, columnId: selectedColumnId, value, text: 'Copied' });
    } catch {
      setCopyStatus({ rowId: row?.id, columnId: selectedColumnId, value,
        text: 'Could not copy. Select the text and copy it manually.' });
    }
  }

  const content = !selectedColumn ? (
    <p className={styles.empty}>Choose a column to show alongside the document.</p>
  ) : !rowId ? (
    <p className={styles.empty}>Choose a document to view its value.</p>
  ) : hydrated.key !== hydrationKey || hydrated.loading ? (
    <p className={styles.empty}>Loading value…</p>
  ) : hydrated.error ? (
    <p className={styles.empty} role="alert">{hydrated.error}</p>
  ) : !row ? (
    <p className={styles.empty}>The document is no longer available.</p>
  ) : (
    <FieldValue key={`${row.id}:${selectedColumn.id}`} col={selectedColumn} columns={columns}
      row={row} sheetId={sheetId} value={value} />
  );

  return (
    <aside className={styles.pane} style={{ '--alongside-width': `${width}px` } as CSSProperties}
      data-testid="document-alongside-pane" aria-labelledby={headingId}>
      <ResizeSeam className={styles.seam} testId="document-alongside-resize" ariaLabel="Resize alongside panel"
        width={width} min={MIN_WIDTH} max={MAX_WIDTH} onResizeStart={onResizeStart} onResizeKeyDown={onResizeKeyDown} />
      <header className={styles.header}>
        <div className={styles.headingRow}>
          <h2 id={headingId} title={selectedColumn?.name}>{selectedColumn?.name ?? 'Show alongside'}</h2>
          {selectedColumn && <button ref={changeRef} type="button" className="icon-btn" aria-label="Change column" title="Change column"
            aria-expanded={choosing} onClick={() => setChoosing(!choosing)}><Columns2 size={15} /></button>}
          <button type="button" className="icon-btn" aria-label="Copy value" title={status === 'Copied' ? 'Copied' : 'Copy value'}
            disabled={value === null || value === ''} onClick={() => void copyValue()}>
            {status === 'Copied' ? <Check size={15} /> : <Copy size={15} />}
          </button>
          <button type="button" className="icon-btn" aria-label="Expand value" title="Expand value"
            disabled={!selectedColumn || !row} onClick={() => setExpanded(true)}><Maximize2 size={15} /></button>
          <button type="button" className="icon-btn" aria-label="Close alongside panel" title="Close alongside panel"
            onClick={onClose}><X size={15} /></button>
        </div>
        {chooserVisible && (
          <PanelSelect ref={pickerRef} ariaLabel="Column to show alongside" testId="document-alongside-column-select"
            value={selectedColumn?.id ?? ''} onValueChange={(columnId) => {
              onChangeColumn(columnId || null); setChoosing(false);
            }} options={[{ value: '', label: 'Choose a column' }, ...columns.map((column) => ({
              value: column.id, label: column.name,
            }))]} />
        )}
        <span className={status && status !== 'Copied' ? styles.copyError : 'sr-only'} role="status">{status}</span>
      </header>
      <div className={styles.body} data-testid="document-alongside-value">{!expanded && content}</div>
      <dialog ref={dialog} className={`modal-card ${styles.expanded}`} aria-labelledby={expandedHeadingId}
        onCancel={(event) => { event.preventDefault(); setExpanded(false); }}>
        <header className={styles.headingRow}>
          <h2 id={expandedHeadingId}>{selectedColumn?.name}</h2>
          <button type="button" className="icon-btn" aria-label="Close expanded value" title="Close expanded value"
            onClick={() => setExpanded(false)}><X size={16} /></button>
        </header>
        <div className={styles.body}>{expanded && content}</div>
      </dialog>
    </aside>
  );
}
