import type { ExtractedCell, ExtractionPreview, ExtractionPreviewDocument } from '../../api/documentExtraction';
import type { ExtractionTemplate } from './types';
import styles from './ExtractView.module.css';

export function ExtractPreview({ template, preview, onSelect }: {
  template: ExtractionTemplate; preview: ExtractionPreview;
  onSelect(document: ExtractionPreviewDocument, cell: ExtractedCell | null): void;
}) {
  return <div className={styles.previewTable}>
    <p role="status">Preview sample: {preview.documents.length} documents.{preview.truncated ? ' Not all selected documents or rows are shown.' : ''}</p>
    <table aria-label="Extraction preview"><thead><tr><th>Document</th>{template.fields.map((field) => <th key={field.id}>{field.name}</th>)}</tr></thead>
      <tbody>{preview.documents.flatMap((document) => {
        const warnings = document.result.diagnostics;
        const warningRows = warnings.length || document.result.records.length === 0
          ? [<tr key={`${document.row_id}-status`}><td><button type="button" onClick={() => onSelect(document, null)}>{document.filename}</button></td>
            <td colSpan={Math.max(1, template.fields.length)} className={document.result.outcome === 'error' ? styles.unclear : styles.empty}>
              {document.result.outcome === 'zero_records' ? `No matching records${warnings.length ? ` · ${warnings.join(' · ')}` : ''}` : warnings.join(' · ') || (document.result.outcome === 'error' ? 'Could not extract this document' : 'No matching records')}
            </td></tr>] : [];
        return [...document.result.records.map((record, index) => <tr key={`${document.row_id}-${index}`}>
          <td><button type="button" onClick={() => onSelect(document, null)}>{index === 0 ? document.filename : `↳ Record ${index + 1}`}</button></td>
          {template.fields.map((field) => {
            const cell = record.cells[field.id];
            return <td key={field.id} className={cell?.status === 'not_found' || !cell ? styles.notFound : index > 0 && !field.section_id ? styles.continuation : ''}>
              <button type="button" title={cell?.diagnostic ?? undefined} onClick={() => onSelect(document, cell ?? null)}>
                {cell?.diagnostic && cell.status !== 'not_found' && <span className={styles.warning} aria-label={cell.diagnostic}>⚠ </span>}
                {!cell || cell.status === 'not_found' ? '⚠ not found' : cell.status === 'empty' ? <em className={styles.empty}>empty</em> : cell.text}
              </button>
            </td>;
          })}
        </tr>), ...warningRows];
      })}</tbody>
    </table>
    {preview.documents.length === 0 && <p>No documents in this sample.</p>}
  </div>;
}
