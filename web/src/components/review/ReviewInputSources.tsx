import { lazy, Suspense, useState } from 'react';
import type { ColumnDef, ReviewBundle, Row } from '../../api/types';
import { FieldValue } from '../rowInspection/FieldValue';
import { OverflowRow } from '../OverflowRow';
import { PanelLoading } from '../PanelPrimitives';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { resolveMediaValue } from '../../media/resolveMediaValue';
import { documentMediaKind } from '../../workbench/documentMedia';
import { ReviewExcerpt } from './ReviewExcerpt';
import styles from './ReviewSourcePreview.module.css';

const PdfViewer = lazy(() => import('../../media/PdfViewer').then(({ PdfViewer }) => ({ default: PdfViewer })));
const ignorePdfEvent = () => undefined;

/** Typed input fallback for runs without stored citations. These are explicitly
 * current values; saved evidence remains the authoritative run-time source. */
export function ReviewInputSources({ bundle }: { bundle: Pick<ReviewBundle, 'sources' | 'rowId' | 'rowIndex' | 'sheetId'> }) {
  const sources = bundle.sources ?? [];
  const { chromePreferences: { projectId } } = useWorkspaceStores();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const pdfUrl = (source: (typeof sources)[number]): string | null => {
    const media = resolveMediaValue(source.value, projectId);
    return documentMediaKind(media, source.columnType) === 'pdf' ? media!.url : null;
  };
  // The document is what a reviewer checks a result against, so it opens first.
  const selected = sources.find((source) => source.columnId === selectedId)
    ?? sources.find((source) => pdfUrl(source) !== null) ?? sources[0];
  const selectedPdfUrl = selected ? pdfUrl(selected) : null;
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
    <div className={selectedPdfUrl ? `${styles.viewer} ${styles.viewerDocument}` : styles.viewer}>
      <p className={styles.inputNotice}>Current input · no saved citation for this result</p>
      {selectedPdfUrl
        ? <div className={styles.inputPdf} data-testid="review-input-pdf" role="document" aria-label={`PDF: ${selected!.columnName}`}>
          <Suspense fallback={<PanelLoading label="Loading PDF…" />}>
            <PdfViewer key={selectedPdfUrl} url={selectedPdfUrl} layout="continuous" fit="width" zoom={1} textLayer
              currentPage={1} onLoaded={ignorePdfEvent} onCurrentPageChange={ignorePdfEvent} />
          </Suspense>
        </div>
        : column && selected && <div className={styles.inputValue}>{selected.boundedValue
          ? <ReviewExcerpt excerpt={selected.boundedValue} />
          : <FieldValue col={column} columns={columns} row={row} sheetId={bundle.sheetId} value={selected.value} />}</div>}
    </div>
  </section>;
}
