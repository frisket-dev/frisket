import { Trash2 } from 'lucide-react';
import type { ExtractionPreview } from '../../api/documentExtraction';
import { textInRegion, type AnnotationTarget, type ExtractionTemplate, type PositionedDocument } from './types';
import styles from './ExtractView.module.css';

export function ExtractFields({ template, reference, selected, preview, onChange, onSelect, onDelete }: {
  template: ExtractionTemplate; reference: PositionedDocument | null; selected: AnnotationTarget | null;
  preview: ExtractionPreview | null; onChange(template: ExtractionTemplate): void;
  onSelect(target: AnnotationTarget): void; onDelete(target: AnnotationTarget): void;
}) {
  const field = (id: string) => {
    const item = template.fields.find((candidate) => candidate.id === id)!;
    const key = textInRegion(reference, item.key);
    const value = textInRegion(reference, item.value);
    return <div key={id} className={`${styles.field} ${selected?.id === id ? styles.activeField : ''}`}>
      <div className={styles.fieldHeading}>
        <button type="button" className="icon-btn" aria-label={`Locate ${item.name}`} onClick={() => onSelect({ kind: 'value', id })}>⌁</button>
        <input aria-label={`Column name for ${item.name}`} className={`form-input ${styles.fieldName}`} value={item.name}
          onChange={(event) => onChange({ ...template, fields: template.fields.map((entry) => entry.id === id ? { ...entry, name: event.target.value } : entry) })} />
        <button type="button" className="icon-btn" aria-label={`Delete ${item.name}`} onClick={() => onDelete({ kind: 'value', id })}><Trash2 size={13} /></button>
      </div>
      <button type="button" className={styles.fieldExample} onClick={() => onSelect({ kind: 'value', id })}>
        <span>{key || '(key)'}</span><span aria-hidden> → </span>{value || <em>empty in this example</em>}
      </button>
      {preview?.documents.flatMap((document) => document.result.records.flatMap((record, index) => {
        const cell = record.cells[id];
        return cell?.status === 'not_found' ? [<p key={`${document.row_id}-${index}`} className={styles.warning}>{document.filename}: {cell.diagnostic ?? 'Field not found'}</p>] : [];
      }))}
    </div>;
  };
  return <aside className={styles.fields} aria-label="Extraction fields">
    <header><h3>Fields</h3><span className="muted">{template.fields.length} columns</span></header>
    {template.fields.length === 0 && <p className={styles.empty}>Draw a box around a key, then its value. These pairs become columns.</p>}
    {template.fields.filter((item) => !item.section_id).map((item) => field(item.id))}
    {template.sections.map((section) => <section key={section.id} className={styles.sectionFields}>
      <div className={styles.fieldHeading}>
        <input aria-label="Repeated section name" className={`form-input ${styles.fieldName}`} value={section.name}
          onChange={(event) => onChange({ ...template, sections: template.sections.map((item) => item.id === section.id ? { ...item, name: event.target.value } : item) })} />
        <button type="button" className="icon-btn" aria-label={`Delete ${section.name}`} onClick={() => onDelete({ kind: 'first', id: section.id })}><Trash2 size={13} /></button>
      </div>
      <button type="button" className={styles.fieldExample} onClick={() => onSelect({ kind: 'first', id: section.id })}>Record 1 · page {section.first.start.page}</button>
      <label className={styles.endPage}>First record ends on page <input type="number" className="form-input"
        min={section.first.start.page} max={section.rest.start.page} value={section.first.end.page}
        onChange={(event) => {
          const page = Number(event.target.value);
          if (!Number.isInteger(page) || page < section.first.start.page || page > section.rest.start.page) return;
          const y = page === section.rest.start.page ? section.rest.start.y : 1;
          if (page === section.first.start.page && y <= section.first.start.y) return;
          onChange({ ...template, sections: template.sections.map((item) => item.id === section.id ? { ...item,
            first: { ...item.first, end: { page, y } } } : item) });
        }} /></label>
      <button type="button" className={styles.fieldExample} onClick={() => onSelect({ kind: 'rest', id: section.id })}>Records 2–end · pages {section.rest.start.page}–{section.rest.end.page}</button>
      <label className={styles.endPage}>Remaining records end on page <input type="number" className="form-input" min={section.rest.start.page}
        max={reference?.pages.length ?? section.rest.end.page} value={section.rest.end.page}
        onChange={(event) => {
          const page = Number(event.target.value);
          if (!Number.isInteger(page) || page < section.rest.start.page || page > (reference?.pages.length ?? page)) return;
          onChange({ ...template, sections: template.sections.map((item) => item.id === section.id ? { ...item,
            rest: { ...item.rest, end: { page, y: page === section.rest.start.page ? Math.max(item.rest.end.y, item.rest.start.y + 0.01) : 1 } } } : item) });
        }} /></label>
      {template.fields.filter((item) => item.section_id === section.id).map((item) => field(item.id))}
    </section>)}
    {template.ignore_bands.length > 0 && <section className={styles.ignored}><h4>Ignored on each page</h4>
      {template.ignore_bands.map((_, index) => <div className={styles.fieldHeading} key={index}>
        <button type="button" className={styles.fieldExample} onClick={() => onSelect({ kind: 'ignore', id: String(index) })}>Ignored region {index + 1}</button>
        <button type="button" className="icon-btn" aria-label={`Delete ignored region ${index + 1}`} onClick={() => onDelete({ kind: 'ignore', id: String(index) })}><Trash2 size={13} /></button>
      </div>)}
    </section>}
  </aside>;
}
