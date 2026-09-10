import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';

/**
 * One home for popover viewport-positioning math. Six sites hand-rolled the
 * same viewport clamp + flip-above + maxHeight — including a byte-identical
 * clamp line in MultiColumnPicker/PanelSelect and a behavioral fork in
 * ModelPicker. They now share these primitives and the `useAnchoredPosition`
 * hook that drives the ref-anchored flip popovers.
 */

/** A resolved fixed-position placement for a portaled/floating menu. */
export interface AnchoredPosition {
  /** Present when the menu opens downward (anchored to the trigger's bottom). */
  top?: number;
  /** Present when the menu flips upward (anchored to the trigger's top). */
  bottom?: number;
  left: number;
  width: number;
  maxHeight: number;
}

export interface AnchoredPositionOptions {
  /** Menu width in px, or a function of the live anchor rect + viewport width. */
  width: number | ((rect: DOMRect, viewportWidth: number) => number);
  /** Vertical gap between the anchor edge and the menu. Default 0. */
  gap?: number;
  /**
   * Horizontal anchoring: `'left'` aligns the menu's left edge to the anchor's
   * left; `'right'` aligns its right edge to the anchor's right (then extends
   * leftward). Default `'left'`.
   */
  align?: 'left' | 'right';
  /** Floor for the computed maxHeight. Default 180. */
  minHeight?: number;
  /** Viewport safe-area margin. Default 8. */
  margin?: number;
}

const VIEWPORT_MARGIN = 8;
/**
 * Below this much room the menu is "cramped" and may flip upward. The single
 * canonical flip predicate resolves the former ModelPicker fork (pure
 * `spaceBelow >= spaceAbove`) onto the MultiColumnPicker/PanelSelect threshold
 * form: prefer opening downward; flip up only when the space below is both under
 * this floor AND smaller than the space above.
 */
const PREFER_BELOW_MIN_HEIGHT = 280;

/**
 * Clamp a preferred left offset so a `width`-wide menu stays inside the viewport
 * with `margin` on both edges. This is the byte-identical clamp line the survey
 * named, centralized: `Math.max(8, Math.min(rect.left, innerWidth - width - 8))`.
 */
export function clampLeft(
  preferredLeft: number,
  width: number,
  viewportWidth: number = typeof window !== 'undefined' ? window.innerWidth : 1200,
  margin: number = VIEWPORT_MARGIN,
): number {
  return Math.max(margin, Math.min(preferredLeft, viewportWidth - width - margin));
}

/**
 * Cap a menu's height to the room below its `top`, never below `minHeight`.
 * Matches the App.tsx ColumnHeaderMenu "space-below" cap.
 */
export function clampMaxHeightBelow(
  top: number,
  minHeight: number,
  viewportHeight: number = typeof window !== 'undefined' ? window.innerHeight : 900,
  margin = 12,
): number {
  return Math.max(minHeight, viewportHeight - top - margin);
}

/**
 * Pure placement resolver: given the anchor rect and options, clamp horizontally
 * and choose down-vs-up placement with the single canonical flip predicate.
 * Exported as the hook module's pure geometry primitive so placement can be
 * exercised without mounting React.
 */
export function resolveAnchoredPosition(
  rect: DOMRect,
  opts: AnchoredPositionOptions,
  viewportWidth: number = typeof window !== 'undefined' ? window.innerWidth : 1200,
  viewportHeight: number = typeof window !== 'undefined' ? window.innerHeight : 900,
): AnchoredPosition {
  const margin = opts.margin ?? VIEWPORT_MARGIN;
  const gap = opts.gap ?? 0;
  const minHeight = opts.minHeight ?? 180;
  const width = typeof opts.width === 'function' ? opts.width(rect, viewportWidth) : opts.width;
  const preferredLeft = opts.align === 'right' ? rect.right - width : rect.left;
  const left = clampLeft(preferredLeft, width, viewportWidth, margin);
  const spaceBelow = viewportHeight - rect.bottom - 12;
  const spaceAbove = rect.top - 12;
  if (spaceBelow >= Math.min(PREFER_BELOW_MIN_HEIGHT, spaceAbove) || spaceBelow >= spaceAbove) {
    return { top: rect.bottom + gap, left, width, maxHeight: Math.max(minHeight, spaceBelow) };
  }
  return {
    bottom: viewportHeight - rect.top + gap,
    left,
    width,
    maxHeight: Math.max(minHeight, spaceAbove),
  };
}

/**
 * How far to translateX a rendered floating element so it stays inside the
 * viewport — the OcrCompareTab Configure-popover horizontal shift. `currentShift`
 * is the transform already applied, so the natural (unshifted) rect can be
 * recovered before measuring overflow.
 */
export function horizontalViewportShift(
  rect: DOMRect,
  currentShift: number,
  margin: number = VIEWPORT_MARGIN,
  viewportWidth: number = typeof window !== 'undefined' ? window.innerWidth : 1200,
): number {
  const naturalLeft = rect.left - currentShift;
  const naturalRight = rect.right - currentShift;
  const overflowRight = naturalRight - (viewportWidth - margin);
  const overflowLeft = margin - naturalLeft;
  if (overflowRight > 0) return -overflowRight;
  if (overflowLeft > 0) return overflowLeft;
  return 0;
}

/**
 * Position a ref-anchored floating menu: measures the anchor on open and on
 * resize/scroll, returning a resolved `AnchoredPosition` (or `null` while
 * disabled). Pair with a portal + fixed positioning so the menu escapes any
 * ancestor `overflow: hidden`.
 */
export function useAnchoredPosition(
  anchorRef: RefObject<HTMLElement | null>,
  opts: AnchoredPositionOptions & { enabled: boolean },
): AnchoredPosition | null {
  const [position, setPosition] = useState<AnchoredPosition | null>(null);
  // Latest options + ref without re-subscribing the reflow listeners on every
  // render (width may be a fresh inline arrow). Synced in an effect, not during
  // render; measure() only reads it post-commit. The stale position while
  // disabled is never rendered — every consumer gates its portal on `open`.
  const latest = useRef({ anchorRef, opts });
  useEffect(() => {
    latest.current = { anchorRef, opts };
  });

  useLayoutEffect(() => {
    if (!opts.enabled) return undefined;
    const measure = () => {
      const el = latest.current.anchorRef.current;
      if (!el) return;
      setPosition(resolveAnchoredPosition(el.getBoundingClientRect(), latest.current.opts));
    };
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
    // `enabled` decides whether listeners exist; anchorRef/opts read live from
    // `latest`, so nothing else belongs in the dep list.
  }, [opts.enabled]);

  return position;
}
