import { useRef, useState, type HTMLAttributes, type PointerEvent } from 'react';
import { moveRegion, normalizeRegion, regionStyle, type RegionBox, type RegionPoint } from './regionGeometry';
import styles from './PageRegion.module.css';

interface Drag { start: RegionPoint; box: RegionBox; frame: DOMRect; corner?: string; pointerId: number }
interface PageRegionProps extends Pick<HTMLAttributes<HTMLElement>, 'className' | 'style' | 'title' | 'children' | 'aria-label' | 'aria-describedby'> {
  box: RegionBox;
  selected?: boolean;
  disabled?: boolean;
  draggable?: boolean;
  resizable?: boolean;
  /** Constrain movement/resizing to the vertical axis, e.g. a full-width band. */
  axis?: 'y';
  onSelect?(): void;
  onChange?(box: RegionBox): void;
  onDelete?(): void;
}

/** Render directly inside a positioned page/image overlay with matching dimensions.
 * Geometry is exact: stroke and handles never enlarge the stored region.
 */
export function PageRegion({ box, selected = false, disabled = false, draggable = false,
  resizable = false, axis, onSelect, onChange, onDelete, className = '', style, children, ...attributes }: PageRegionProps) {
  const drag = useRef<Drag | null>(null);
  const [draft, setDraft] = useState<RegionBox | null>(null);
  const interactive = Boolean(onSelect || onChange || onDelete);
  const point = (event: PointerEvent, frame: DOMRect): RegionPoint => ({
    x: Math.max(0, Math.min(1, (event.clientX - frame.left) / frame.width)),
    y: Math.max(0, Math.min(1, (event.clientY - frame.top) / frame.height)),
  });
  const changed = (current: Drag, end: RegionPoint): RegionBox => current.corner
    ? normalizeRegion(
      { x: axis === 'y' || !current.corner.includes('w') ? current.box.x0 : end.x,
        y: current.corner.includes('n') ? end.y : current.box.y0 },
      { x: axis === 'y' || !current.corner.includes('e') ? current.box.x1 : end.x,
        y: current.corner.includes('s') ? end.y : current.box.y1 })
    : moveRegion(current.box, end.x - current.start.x, end.y - current.start.y, axis);
  const cancel = () => { drag.current = null; setDraft(null); };
  const common = { ...attributes, className: `${styles.region} ${interactive ? styles.editable : styles.readonly} ${className}`,
    style: { ...style, ...regionStyle(draft ?? box) } };
  if (!(box.x1 > box.x0 && box.y1 > box.y0)) return null;
  if (!interactive) return <span {...common}>{children}</span>;
  return <button {...common} type="button" disabled={disabled} aria-pressed={selected}
    onClick={() => { if (!disabled) onSelect?.(); }}
    onPointerDown={(event) => {
      if (disabled || event.button !== 0 || !onChange) return;
      const corner = (event.target as HTMLElement).dataset.corner;
      if (corner ? !resizable : !draggable) return;
      const frame = event.currentTarget.parentElement?.getBoundingClientRect();
      if (!frame?.width || !frame.height) return;
      event.preventDefault(); event.stopPropagation();
      event.currentTarget.setPointerCapture(event.pointerId);
      onSelect?.();
      drag.current = { start: point(event, frame), box, frame, corner, pointerId: event.pointerId };
    }}
    onPointerMove={(event) => {
      const current = drag.current;
      if (current && event.pointerId === current.pointerId) { event.stopPropagation(); setDraft(changed(current, point(event, current.frame))); }
    }}
    onPointerUp={(event) => {
      const current = drag.current;
      if (!current || event.pointerId !== current.pointerId) return;
      event.stopPropagation();
      const next = changed(current, point(event, current.frame));
      cancel();
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
      if (!disabled && next.x1 > next.x0 && next.y1 > next.y0) onChange?.(next);
    }}
    onPointerCancel={cancel} onLostPointerCapture={cancel}
    onKeyDown={(event) => {
      if (disabled) return;
      if ((event.key === 'Delete' || event.key === 'Backspace') && onDelete) {
        event.preventDefault(); event.stopPropagation(); onDelete();
      }
      const delta = event.shiftKey ? 0.01 : 0.002;
      const moves: Record<string, RegionPoint> = { ArrowLeft: { x: -delta, y: 0 }, ArrowRight: { x: delta, y: 0 },
        ArrowUp: { x: 0, y: -delta }, ArrowDown: { x: 0, y: delta } };
      if (draggable && onChange && moves[event.key]) {
        event.preventDefault(); event.stopPropagation();
        onChange(moveRegion(box, moves[event.key].x, moves[event.key].y, axis));
      }
    }}>
    {children}
    {!disabled && selected && resizable && onChange && ['nw', 'ne', 'sw', 'se'].map((corner) =>
      <span key={corner} data-corner={corner} aria-hidden className={`${styles.handle} ${styles[corner]}`} />)}
  </button>;
}
