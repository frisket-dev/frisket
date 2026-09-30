import { useState } from 'react';
import type { ColumnDef, ReviewBundle, Row } from '../../api/types';
import { FieldValue } from '../RowDrawer';
import { OverflowRow } from '../OverflowRow';
import styles from './ReviewSourcePreview.module.css';

/** Typed input fallback for runs without stored citations. These are explicitly
 * current values; saved evidence remains the authoritative run-time source. */
export function ReviewInputSources({ bundle }: { bundle: ReviewBundle }) {
  const sources = bundle.sources ?? [];
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = sources.find((source) => source.columnId === selectedId) ?? sources[0];
  const columns: ColumnDef[] = sources.map((source) => ({
    id: source.columnId, name: source.columnName, type: source.columnType,
    format: source.format, semanticType: source.semanticType,
  }));
  const row: Row = { id: bundle.rowId, index: bundle.rowIndex,
    cells: Object.fromEntries(sources.map((source) => [source.columnId, source.value])), provenance: {} };
  const column = columns.find((item) => item.id === selected?.columnId);
  return <section className={styles.pane} data-testid="review-input-preview" aria-label="Input sources">
    <div className={styles.tabs}>
      <OverflowRow items={sources} getKey={(source) => source.columnId} keepVisibleKey={selected?.columnId}
        className={styles.tabRow} overflowLabel="More input sources"
        renderItem={(source) => <button className={styles.tab} role="tab" aria-selected={selected?.columnId === source.columnId}
          onClick={() => setSelectedId(source.columnId)}>{source.columnName}</button>}
        renderOverflowItem={(source, close) => <button className={styles.tabMenuItem} role="menuitem"
          onClick={() => { setSelectedId(source.columnId); close(); }}>{source.columnName}</button>} />
    </div>
    <div className={styles.viewer}>
      <p className={styles.inputNotice}>Current input · no saved citation for this result</p>
      {column && selected && <div className={styles.inputValue}><FieldValue col={column} columns={columns} row={row}
        sheetId={bundle.sheetId} value={selected.value} /></div>}
    </div>
  </section>;
}
