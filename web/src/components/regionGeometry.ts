import type { CSSProperties } from 'react';

/** Coordinates in the containing page/image, independent of its rendered size. */
export interface RegionBox { x0: number; y0: number; x1: number; y1: number }
export interface RegionPoint { x: number; y: number }

export function normalizeRegion(a: RegionPoint, b: RegionPoint): RegionBox {
  const clamp = (n: number) => Math.max(0, Math.min(1, n));
  return { x0: clamp(Math.min(a.x, b.x)), y0: clamp(Math.min(a.y, b.y)),
    x1: clamp(Math.max(a.x, b.x)), y1: clamp(Math.max(a.y, b.y)) };
}

export function regionStyle(box: RegionBox): CSSProperties {
  return { left: `${box.x0 * 100}%`, top: `${box.y0 * 100}%`,
    width: `${(box.x1 - box.x0) * 100}%`, height: `${(box.y1 - box.y0) * 100}%` };
}

export function moveRegion(box: RegionBox, dx: number, dy: number, axis?: 'y'): RegionBox {
  dx = axis === 'y' ? 0 : Math.max(-box.x0, Math.min(1 - box.x1, dx));
  dy = Math.max(-box.y0, Math.min(1 - box.y1, dy));
  return { x0: box.x0 + dx, x1: box.x1 + dx, y0: box.y0 + dy, y1: box.y1 + dy };
}
