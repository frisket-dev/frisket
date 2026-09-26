import { useLayoutEffect, useRef, useState } from 'react';
import type { SheetMeta } from '../api/open';

const TAB_GAP = 3;
const MORE_TABS_WIDTH = 30;

interface SheetTabLayout {
  sourceIds: string[];
  visibleIds: string[];
}

function tabRowWidth(widths: number[], includesMoreTabs: boolean) {
  const tabsWidth = widths.reduce((sum, width) => sum + width, 0);
  const gapsWidth = Math.max(0, widths.length - 1) * TAB_GAP;
  return tabsWidth + gapsWidth + (includesMoreTabs ? TAB_GAP + MORE_TABS_WIDTH : 0);
}

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
  const [layout, setLayout] = useState<SheetTabLayout | null>(null);

  useLayoutEffect(() => {
    const row = sheetTabsRef.current;
    const measurer = measureRef.current;
    if (!row || !measurer) return undefined;
    const recompute = () => {
      const widths = Array.from(measurer.children).map((node) => (node as HTMLElement).offsetWidth);
      const available = row.clientWidth;
      const overflowNeeded = tabRowWidth(widths, false) > available;
      let visibleIndexes = sheets.map((_, index) => index);
      if (overflowNeeded) {
        visibleIndexes = [];
        for (const [index, width] of widths.entries()) {
          const candidateWidths = [...visibleIndexes.map((visibleIndex) => widths[visibleIndex]), width];
          if (tabRowWidth(candidateWidths, true) <= available) {
            visibleIndexes.push(index);
          } else {
            break;
          }
        }
        // The outer workbench has room for a tab and its More control. Preserve
        // a non-empty, pre-measurement fallback even if a transient zero width
        // arrives while this region is mounting.
        if (visibleIndexes.length === 0 && sheets.length > 0) visibleIndexes = [0];
        const selectedIndex = selectedSheetIsActive
          ? sheets.findIndex((sheet) => sheet.id === selectedSheet?.id)
          : -1;
        if (selectedIndex >= 0 && !visibleIndexes.includes(selectedIndex)) {
          const retainedIndexes = visibleIndexes.slice(0, -1);
          while (
            retainedIndexes.length > 0
            && tabRowWidth([...retainedIndexes.map((index) => widths[index]), widths[selectedIndex]], true) > available
          ) {
            retainedIndexes.pop();
          }
          visibleIndexes = [...retainedIndexes, selectedIndex];
        }
      }
      const visibleIds = visibleIndexes.map((index) => sheets[index].id);
      const sourceIds = sheets.map((sheet) => sheet.id);
      setVisibleCount((current) => current === visibleIds.length ? current : visibleIds.length);
      setLayout((current) => (
        current && current.sourceIds.every((id, index) => id === sourceIds[index])
          && current.sourceIds.length === sourceIds.length
          && current.visibleIds.every((id, index) => id === visibleIds[index])
          && current.visibleIds.length === visibleIds.length
          ? current
          : { sourceIds, visibleIds }
      ));
    };
    const observer = new ResizeObserver(recompute);
    observer.observe(row);
    recompute();
    return () => observer.disconnect();
  }, [sheets, selectedSheet?.id, selectedSheetIsActive]);

  const currentLayout = layout
    && layout.sourceIds.length === sheets.length
    && layout.sourceIds.every((id, index) => id === sheets[index].id)
    ? layout
    : null;
  const fallbackCount = sheets.length === 0 ? 0 : Math.max(1, Math.min(visibleCount, sheets.length));
  const visibleIds = new Set(currentLayout?.visibleIds ?? sheets.slice(0, fallbackCount).map((sheet) => sheet.id));
  const visibleSheets = sheets.filter((sheet) => visibleIds.has(sheet.id));
  const overflowSheets = sheets.filter((sheet) => !visibleIds.has(sheet.id));
  return { sheetTabsRef, measureRef, visibleSheets, overflowSheets };
}
