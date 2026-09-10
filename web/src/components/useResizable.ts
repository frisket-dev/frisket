import { useState } from 'react';

/** Which edge of the panel carries the drag handle. left/right resize width;
 *  top/bottom resize height. Dragging away from the fixed opposite edge grows. */
export type ResizableEdge = 'left' | 'right' | 'top' | 'bottom';

export interface ResizableOptions {
  /** localStorage key the size persists under. */
  storageKey: string;
  minWidth: number;
  maxWidth: number;
  defaultWidth: number;
  handleEdge: ResizableEdge;
}

function loadWidth({ storageKey, minWidth, maxWidth, defaultWidth }: ResizableOptions): number {
  const n = Number(localStorage.getItem(storageKey));
  return Number.isFinite(n) && n > 0 ? Math.min(maxWidth, Math.max(minWidth, n)) : defaultWidth;
}

function isVertical(edge: ResizableEdge): boolean {
  return edge === 'top' || edge === 'bottom';
}

// top/left keep the far edge fixed, so dragging toward the smaller coordinate
// grows the panel (negative delta = larger).
function growsOnNegativeDelta(edge: ResizableEdge): boolean {
  return edge === 'top' || edge === 'left';
}

function growKey(edge: ResizableEdge): string {
  switch (edge) {
    case 'top':
      return 'ArrowUp';
    case 'bottom':
      return 'ArrowDown';
    case 'left':
      return 'ArrowLeft';
    default:
      return 'ArrowRight';
  }
}

function shrinkKey(edge: ResizableEdge): string {
  switch (edge) {
    case 'top':
      return 'ArrowDown';
    case 'bottom':
      return 'ArrowUp';
    case 'left':
      return 'ArrowRight';
    default:
      return 'ArrowLeft';
  }
}

/**
 * Drag-to-resize for a fixed-edge panel (drawers, the action panel, the bottom
 * dock). Wire `onResizeStart` to a handle element on `handleEdge`; the chosen
 * size persists in localStorage under `storageKey`. `width` is the resized
 * dimension — the panel's width for left/right, its height for top/bottom.
 */
export function useResizable(opts: ResizableOptions) {
  const [width, setWidth] = useState(() => loadWidth(opts));
  const [resizing, setResizing] = useState(false);
  const setPersistedWidth = (nextWidth: number) => {
    const clamped = Math.min(opts.maxWidth, Math.max(opts.minWidth, nextWidth));
    setWidth(clamped);
    localStorage.setItem(opts.storageKey, String(clamped));
  };

  const vertical = isVertical(opts.handleEdge);
  const growNeg = growsOnNegativeDelta(opts.handleEdge);

  const onResizeStart = (e: React.PointerEvent<HTMLElement>) => {
    e.preventDefault();
    const start = vertical ? e.clientY : e.clientX;
    const startW = width;
    let w = startW;
    setResizing(true);
    const onMove = (ev: PointerEvent) => {
      const delta = (vertical ? ev.clientY : ev.clientX) - start;
      w = Math.min(
        opts.maxWidth,
        Math.max(opts.minWidth, growNeg ? startW - delta : startW + delta),
      );
      setWidth(w);
    };
    const onUp = () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      setResizing(false);
      setPersistedWidth(w);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  };

  const grow = growKey(opts.handleEdge);
  const shrink = shrinkKey(opts.handleEdge);
  const onResizeKeyDown = (e: React.KeyboardEvent<HTMLElement>) => {
    if (e.key !== grow && e.key !== shrink) return;
    e.preventDefault();
    const step = e.shiftKey ? 48 : 24;
    setPersistedWidth(width + (e.key === grow ? step : -step));
  };

  return { width, resizing, onResizeStart, onResizeKeyDown };
}
