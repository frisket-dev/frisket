import { useLayoutEffect, useRef, useState } from 'react';
import type { SheetMeta } from '../api/open';

/** Measures the sheet-only portion of the main tab strip and keeps a selected
 * sheet reachable when its neighbors move into the overflow menu. */
export function useSheetTabsOverflow(
  sheets: SheetMeta[],
  selectedSheet: SheetMeta | undefined,
  selectedSheetIsActive: boolean,
) {
  const sheetTabsRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLDivElement>(null);
  const [visibleCount, setVisibleCount] = useState(sheets.length);

  useLayoutEffect(() => {
    const row = sheetTabsRef.current;
    const measurer = measureRef.current;
    if (!row || !measurer) return undefined;
    const recompute = () => {
      const widths = Array.from(measurer.children).map((node) => (node as HTMLElement).offsetWidth);
      const available = row.clientWidth;
      const total = widths.reduce((sum, width) => sum + width, 0);
      let next = sheets.length;
      if (total > available) {
        const moreTabsWidth = 34;
        let used = 0;
        next = 0;
        for (const width of widths) {
          used += width;
          if (used + moreTabsWidth <= available) next += 1;
          else break;
        }
        next = Math.max(1, Math.min(sheets.length, next));
      }
      setVisibleCount((current) => current === next ? current : next);
    };
    const observer = new ResizeObserver(recompute);
    observer.observe(row);
    recompute();
    return () => observer.disconnect();
  }, [sheets, selectedSheet?.id, selectedSheetIsActive]);

  let visibleSheets = sheets.slice(0, visibleCount);
  let overflowSheets = sheets.slice(visibleCount);
  if (selectedSheetIsActive && selectedSheet && overflowSheets.some((sheet) => sheet.id === selectedSheet.id)) {
    const displaced = visibleSheets[visibleSheets.length - 1];
    visibleSheets = [...visibleSheets.slice(0, -1), selectedSheet];
    overflowSheets = [displaced, ...overflowSheets.filter((sheet) => sheet.id !== selectedSheet.id)];
  }
  return { sheetTabsRef, measureRef, visibleSheets, overflowSheets };
}
