import {
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from 'react';

interface OverflowLayout {
  sourceIds: string[];
  visibleIds: string[];
}

export interface OverflowItemsOptions<T> {
  getKey(item: T): string;
  /** Reserve a currently selected item in the visible row whenever possible. */
  keepVisibleKey?: string;
  /** A hidden or visible copy of the overflow trigger, measured after mount. */
  overflowControlRef?: RefObject<HTMLElement | null>;
  /** Used when the caller has no trigger copy to measure. */
  overflowControlWidth?: number;
}

function rowWidth(widths: number[], gap: number, triggerWidth: number, includesTrigger: boolean) {
  const itemsWidth = widths.reduce((sum, width) => sum + width, 0);
  const gapsWidth = Math.max(0, widths.length - 1) * gap;
  return itemsWidth + gapsWidth + (includesTrigger && widths.length > 0 ? gap + triggerWidth : 0);
}

function sameLayout(current: OverflowLayout | null, sourceIds: string[], visibleIds: string[]) {
  return current
    && current.sourceIds.length === sourceIds.length
    && current.sourceIds.every((id, index) => id === sourceIds[index])
    && current.visibleIds.length === visibleIds.length
    && current.visibleIds.every((id, index) => id === visibleIds[index]);
}

/**
 * Computes the split for a no-scroll item row. Callers render the measure row
 * with the same markup as their visible items, so arbitrary element content is
 * measured accurately.
 */
export function useOverflowItems<T>(
  items: readonly T[],
  { getKey, keepVisibleKey, overflowControlRef, overflowControlWidth = 0 }: OverflowItemsOptions<T>,
) {
  const rowRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLDivElement>(null);
  const [layout, setLayout] = useState<OverflowLayout | null>(null);

  useLayoutEffect(() => {
    const row = rowRef.current;
    const measurer = measureRef.current;
    if (!row || !measurer) return undefined;

    const recompute = () => {
      const widths = Array.from(measurer.children).map((node) => (node as HTMLElement).offsetWidth);
      const parsedGap = Number.parseFloat(getComputedStyle(measurer).columnGap);
      const gap = Number.isFinite(parsedGap) ? parsedGap : 0;
      const available = row.clientWidth;
      const triggerWidth = overflowControlRef?.current?.offsetWidth ?? overflowControlWidth;
      let visibleIndexes = items.map((_, index) => index);
      if (rowWidth(widths, gap, triggerWidth, false) > available) {
        visibleIndexes = [];
        for (const [index, width] of widths.entries()) {
          const candidateWidths = [...visibleIndexes.map((visibleIndex) => widths[visibleIndex]), width];
          if (rowWidth(candidateWidths, gap, triggerWidth, true) <= available) {
            visibleIndexes.push(index);
          } else {
            break;
          }
        }
        const activeIndex = items.findIndex((item) => getKey(item) === keepVisibleKey);
        if (activeIndex >= 0 && !visibleIndexes.includes(activeIndex)
          && rowWidth([widths[activeIndex]], gap, triggerWidth, true) <= available) {
          const retainedIndexes = [...visibleIndexes];
          while (
            retainedIndexes.length > 0
            && rowWidth(
              [...retainedIndexes.map((index) => widths[index]), widths[activeIndex]],
              gap,
              triggerWidth,
              true,
            ) > available
          ) {
            retainedIndexes.pop();
          }
          visibleIndexes = [...retainedIndexes, activeIndex];
        }
      }
      const sourceIds = items.map(getKey);
      const visibleIds = visibleIndexes.map((index) => getKey(items[index]));
      setLayout((current) => (sameLayout(current, sourceIds, visibleIds)
        ? current
        : { sourceIds, visibleIds }));
    };

    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(recompute);
    observer?.observe(row);
    observer?.observe(measurer);
    recompute();
    return () => observer?.disconnect();
  }, [getKey, items, keepVisibleKey, overflowControlRef, overflowControlWidth]);

  const sourceIds = items.map(getKey);
  const layoutIsCurrent = layout
    && layout.sourceIds.length === sourceIds.length
    && layout.sourceIds.every((id, index) => id === sourceIds[index]);
  const visibleIds = new Set(layoutIsCurrent ? layout.visibleIds : sourceIds);
  return {
    rowRef,
    measureRef,
    visibleItems: items.filter((item) => visibleIds.has(getKey(item))),
    overflowItems: items.filter((item) => !visibleIds.has(getKey(item))),
  };
}
