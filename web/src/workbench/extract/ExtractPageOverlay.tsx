import { useRef, useState, type PointerEvent } from 'react';
import { normalizeBox, spanOnPage, type AnnotationTarget, type Box, type ExtractionTemplate, type ExtractTool, type PageRegion } from './types';
import styles from './ExtractView.module.css';

interface Region { target: AnnotationTarget; box: Box; label: string; band?: boolean; repeated?: boolean }
interface Drag { start: { x: number; y: number }; box?: Box; target?: AnnotationTarget; corner?: string; band: boolean }
export interface ExtractPageOverlayProps {
  page: number;
  template: ExtractionTemplate;
  tool: ExtractTool;
  selected: AnnotationTarget | null;
  muted?: boolean;
  focusRegions?: PageRegion[];
  pending?: PageRegion | null;
  onSelect(target: AnnotationTarget): void;
  onDraw(region: PageRegion): void;
  onChange(target: AnnotationTarget, region: PageRegion): void;
  onDelete(target: AnnotationTarget): void;
}
const same = (a: AnnotationTarget | null, b: AnnotationTarget) => a?.kind === b.kind && a.id === b.id;

export function ExtractPageOverlay({ page, template, tool, selected, muted = false, focusRegions = [], pending,
  onSelect, onDraw, onChange, onDelete }: ExtractPageOverlayProps) {
  const drag = useRef<Drag | null>(null);
  const [draft, setDraft] = useState<Box | null>(null);
  const regions: Region[] = [
    ...template.ignore_bands.map(({ box }, i): Region => ({ target: { kind: 'ignore', id: String(i) }, box, label: 'Ignored region', band: true })),
    ...template.sections.flatMap((section) => (['first', 'rest'] as const).flatMap((kind) => {
      const box = spanOnPage(section[kind], page);
      return box ? [{ target: { kind, id: section.id }, box, label: kind === 'first' ? `${section.name}: record 1` : `${section.name}: records 2–end`, band: true, repeated: true }] : [];
    })),
    ...template.fields.flatMap((field) => (['key', 'value'] as const).filter((kind) => field[kind].page === page)
      .map((kind): Region => ({ target: { kind, id: field.id }, box: field[kind].box, label: `${field.name} ${kind}`, repeated: field.section_id !== null }))),
  ];
  const point = (event: PointerEvent<HTMLDivElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    return { x: Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)),
      y: Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)) };
  };
  const moveBox = (current: Drag, end: { x: number; y: number }): Box => {
    const box = current.box;
    if (!box) return normalizeBox(current.start, end, current.band);
    if (current.corner) return normalizeBox(
      { x: current.corner.includes('w') ? end.x : box.x0, y: current.corner.includes('n') ? end.y : box.y0 },
      { x: current.corner.includes('e') ? end.x : box.x1, y: current.corner.includes('s') ? end.y : box.y1 }, current.band);
    const dx = current.band ? 0 : Math.max(-box.x0, Math.min(1 - box.x1, end.x - current.start.x));
    const dy = Math.max(-box.y0, Math.min(1 - box.y1, end.y - current.start.y));
    return { x0: box.x0 + dx, x1: box.x1 + dx, y0: box.y0 + dy, y1: box.y1 + dy };
  };
  return <div className={`${styles.overlay} ${muted ? styles.mutedOverlay : ''}`} data-extract-page={page}
    style={{ cursor: !muted && tool !== 'select' ? 'crosshair' : undefined }}
    onPointerDown={(event) => {
      if (muted || event.button !== 0) return;
      const element = (event.target as HTMLElement).closest<HTMLElement>('[data-region-index]');
      const region = element ? regions[Number(element.dataset.regionIndex)] : undefined;
      if (tool === 'select' && !region) return;
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      if (tool === 'select' && region) {
        onSelect(region.target);
        drag.current = { start: point(event), box: region.box, target: region.target, corner: (event.target as HTMLElement).dataset.corner, band: Boolean(region.band) };
      } else drag.current = { start: point(event), band: tool === 'repeat' || tool === 'ignore' };
    }}
    onPointerMove={(event) => { if (drag.current) setDraft(moveBox(drag.current, point(event))); }}
    onPointerUp={(event) => {
      const current = drag.current;
      if (!current) return;
      const box = moveBox(current, point(event));
      drag.current = null;
      setDraft(null);
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
      if (box.x1 - box.x0 < 0.003 || box.y1 - box.y0 < 0.003) return;
      if (current.target) onChange(current.target, { page, box });
      else onDraw({ page, box });
    }}
    onPointerCancel={() => { drag.current = null; setDraft(null); }}>
    <svg className={styles.connectors} viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden>
      {template.fields.filter((field) => field.key.page === page && field.value.page === page).map((field) =>
        <line key={field.id} x1={field.key.box.x1} y1={(field.key.box.y0 + field.key.box.y1) / 2}
          x2={field.value.box.x0} y2={(field.value.box.y0 + field.value.box.y1) / 2}
          stroke={field.section_id ? '#1F7A5A' : '#4B4DDB'} strokeWidth="1" vectorEffect="non-scaling-stroke" />)}
    </svg>
    {regions.map((region, index) => <button key={`${region.target.kind}:${region.target.id}`} type="button"
      data-region-index={index} aria-label={region.label} aria-pressed={same(selected, region.target)}
      tabIndex={muted ? -1 : 0} disabled={muted}
      className={`${styles.region} ${styles[region.target.kind]} ${region.repeated ? styles.repeated : ''}`}
      style={boxStyle(region.box)} onClick={() => { if (!muted && tool === 'select') onSelect(region.target); }}
      onKeyDown={(event) => {
        if (muted) return;
        if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); onDelete(region.target); }
        const delta = event.shiftKey ? 0.01 : 0.002;
        const moves: Record<string, { x: number; y: number }> = { ArrowLeft: { x: -delta, y: 0 }, ArrowRight: { x: delta, y: 0 }, ArrowUp: { x: 0, y: -delta }, ArrowDown: { x: 0, y: delta } };
        if (moves[event.key]) { event.preventDefault(); onChange(region.target, { page,
          box: moveBox({ start: { x: 0, y: 0 }, box: region.box, band: Boolean(region.band) }, moves[event.key]) }); }
      }}>
      {!muted && region.target.kind !== 'key' && <span className={styles.regionLabel}>{region.label.replace(/ value$/, '')}</span>}
      {!muted && same(selected, region.target) && ['nw', 'ne', 'sw', 'se'].map((corner) => <span key={corner} data-corner={corner} className={`${styles.handle} ${styles[corner]}`} />)}
    </button>)}
    {pending?.page === page && <div className={`${styles.region} ${styles.draft}`} style={boxStyle(pending.box)} />}
    {draft && <div className={`${styles.region} ${styles.draft}`} style={boxStyle(draft)} />}
    {focusRegions.filter((region) => region.page === page).map((region, index) => <div key={index} className={`${styles.region} ${styles.focusRegion}`} style={boxStyle(region.box)} />)}
  </div>;
}

function boxStyle(box: Box) {
  return { left: `${box.x0 * 100}%`, top: `${box.y0 * 100}%`, width: `${(box.x1 - box.x0) * 100}%`, height: `${(box.y1 - box.y0) * 100}%` };
}
