import { useRef, useState, type PointerEvent } from 'react';
import { normalizeBox, spanOnPage, type AnnotationTarget, type Box, type ExtractionTemplate, type ExtractTool, type PageRegion } from './types';
import { PageRegion as Region } from '../../components/PageRegion';
import styles from './ExtractView.module.css';

interface DrawnRegion { target: AnnotationTarget; box: Box; label: string; band?: boolean; repeated?: boolean }
interface Drag { start: { x: number; y: number }; band: boolean; pointerId: number }
export interface ExtractPageOverlayProps {
  page: number;
  template: ExtractionTemplate;
  tool: ExtractTool;
  selected: AnnotationTarget | null;
  muted?: boolean;
  showTemplate?: boolean;
  resultFields?: Array<{ id: string; name: string; regions: PageRegion[] }>;
  focusedResultFieldId?: string | null;
  pending?: PageRegion | null;
  onSelect(target: AnnotationTarget): void;
  onDraw(region: PageRegion): void;
  onChange(target: AnnotationTarget, region: PageRegion): void;
  onDelete(target: AnnotationTarget): void;
}
const same = (a: AnnotationTarget | null, b: AnnotationTarget) => a?.kind === b.kind && a.id === b.id;

export function ExtractPageOverlay({ page, template, tool, selected, muted = false, showTemplate = true,
  resultFields = [], focusedResultFieldId = null, pending,
  onSelect, onDraw, onChange, onDelete }: ExtractPageOverlayProps) {
  const drag = useRef<Drag | null>(null);
  const [draft, setDraft] = useState<Box | null>(null);
  const regions: DrawnRegion[] = showTemplate ? [
    ...template.ignore_bands.map(({ box }, i): DrawnRegion => ({ target: { kind: 'ignore', id: String(i) }, box, label: 'Ignored region', band: true })),
    ...template.sections.flatMap((section) => (['first', 'rest'] as const).flatMap((kind) => {
      const box = spanOnPage(section[kind], page);
      return box ? [{ target: { kind, id: section.id }, box, label: kind === 'first' ? `${section.name}: record 1` : `${section.name}: records 2–end`, band: true, repeated: true }] : [];
    })),
    ...template.fields.flatMap((field) => (['key', 'value'] as const).flatMap((kind) => {
      const fieldRegion = field[kind];
      return fieldRegion?.page === page
        ? [{ target: { kind, id: field.id }, box: fieldRegion.box, label: `${field.name} ${kind}`, repeated: field.section_id !== null }]
        : [];
    })),
  ] : [];
  const point = (event: PointerEvent<HTMLDivElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    return { x: Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)),
      y: Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)) };
  };
  return <div className={`${styles.overlay} ${muted ? styles.mutedOverlay : ''}`} data-extract-page={page}
    style={{ cursor: !muted && tool !== 'select' ? 'crosshair' : undefined }}
    onPointerDown={(event) => {
      if (!showTemplate || muted || event.button !== 0 || tool === 'select') return;
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      drag.current = { start: point(event), band: tool === 'repeat' || tool === 'ignore', pointerId: event.pointerId };
    }}
    onPointerMove={(event) => { if (drag.current?.pointerId === event.pointerId) setDraft(normalizeBox(drag.current.start, point(event), drag.current.band)); }}
    onPointerUp={(event) => {
      const current = drag.current;
      if (!current || current.pointerId !== event.pointerId) return;
      const box = normalizeBox(current.start, point(event), current.band);
      drag.current = null;
      setDraft(null);
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
      if (box.x1 - box.x0 < 0.003 || box.y1 - box.y0 < 0.003) return;
      onDraw({ page, box });
    }}
    onPointerCancel={() => { drag.current = null; setDraft(null); }}>
    <svg className={styles.connectors} viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden>
      {showTemplate && template.fields.filter((field) => field.key?.page === page && field.value.page === page).map((field) => field.key &&
        <line key={field.id} x1={field.key.box.x1} y1={(field.key.box.y0 + field.key.box.y1) / 2}
          x2={field.value.box.x0} y2={(field.value.box.y0 + field.value.box.y1) / 2}
          stroke={field.section_id ? '#1F7A5A' : '#4B4DDB'} strokeWidth="1" vectorEffect="non-scaling-stroke" />)}
    </svg>
    {regions.map((region) => <Region key={`${region.target.kind}:${region.target.id}`}
      box={region.box} aria-label={region.label} selected={same(selected, region.target)} disabled={muted}
      draggable={tool === 'select'} resizable={tool === 'select'} axis={region.band ? 'y' : undefined}
      className={`${styles.region} ${styles[region.target.kind]} ${region.repeated ? styles.repeated : ''}`}
      onSelect={() => { if (tool === 'select') onSelect(region.target); }}
      onChange={(box) => onChange(region.target, { page, box })} onDelete={() => onDelete(region.target)}>
      {!muted && region.target.kind !== 'key' && <span className={styles.regionLabel}>{region.label.replace(/ value$/, '')}</span>}
    </Region>)}
    {showTemplate && pending?.page === page && <Region box={pending.box} className={`${styles.region} ${styles.draft}`} />}
    {showTemplate && draft && <Region box={draft} className={`${styles.region} ${styles.draft}`} />}
    {resultFields.flatMap((field) => field.regions.filter((region) => region.page === page).map((region, index) =>
      <Region key={`${field.id}:${index}`} box={region.box} disabled aria-label={`${field.name} extracted value`}
        className={`${styles.region} ${styles.resultRegion} ${field.id === focusedResultFieldId ? styles.focusRegion : ''}`} />))}
  </div>;
}
