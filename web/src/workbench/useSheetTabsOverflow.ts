import type { SheetMeta } from '../api/open';
import { useOverflowItems } from '../components/useOverflowItems';

const sheetKey = (sheet: SheetMeta) => sheet.id;

/** Sheet controls and keyboard navigation stay with the workbench; only the
 * available-width calculation is shared with other overflow rows. */
export function useSheetTabsOverflow(
  sheets: SheetMeta[],
  selectedSheet: SheetMeta | undefined,
  selectedSheetIsActive: boolean,
) {
  const { rowRef, measureRef, visibleItems, overflowItems } = useOverflowItems(sheets, {
    getKey: sheetKey,
    keepVisibleKey: selectedSheetIsActive ? selectedSheet?.id : undefined,
    // Matches the existing sheet More-tabs control's CSS width.
    overflowControlWidth: 30,
  });
  return { sheetTabsRef: rowRef, measureRef, visibleSheets: visibleItems, overflowSheets: overflowItems };
}
