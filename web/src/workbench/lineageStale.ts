// Stale-cascade ordering shared by the tab strip's stale pill and the Monitor
// Lineage tab (kept out of component files for react-refresh).

import type { SheetMeta } from '../api/open';

/** Stale sheets ordered deepest-first (chain length to the root via parent
 *  links) — the display order of the cascade confirm; refresh runs the
 *  REVERSE (shallow→deep) so children re-materialize against fresh parents. */
export function staleSheetsDeepestFirst(sheets: SheetMeta[]): SheetMeta[] {
  const byId = new Map(sheets.map((s) => [s.id, s]));
  const depthOf = (sheet: SheetMeta): number => {
    let depth = 0;
    let cur: SheetMeta | undefined = sheet;
    while (cur?.parent) {
      depth += 1;
      cur = byId.get(cur.parent.sheetId);
    }
    return depth;
  };
  return sheets
    .filter((s) => s.syncState === 'stale')
    .sort((a, b) => depthOf(b) - depthOf(a));
}
