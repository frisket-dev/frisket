// The ONE shared row-title rule, so every surface that shows a row's "name"
// agrees: DocumentView (whose own Title select is now a per-view OVERRIDE of the
// sheet default, not the only source), InspectDetailColumn (the row drawer
// header), and any other title derivation site (e.g. App.tsx's document-view
// sort-direction lookup).
//
// Column resolution priority (explicit override > sheet-level default > grid's
// current drag order > canonical order):
//   1. `overrideColumnId`  — a per-view choice (DocumentView's Title select).
//   2. `sheet.titleColumnId` — the sheet-level default, set via the column
//      '...' menu's "Use as row title" (schema + server-persisted).
//   3. `columnOrder` — the grid's current drag order (an array of column
//      NAMES, e.g. gridViewStore's `columnOrderBySheet[sheet.id]`, ideally
//      pre-filtered to visible columns by the caller), first entry that still
//      resolves to a real column.
//   4. `sheet.columns[0]` — canonical column order, the last-resort default.
//
// Graph/timeline heuristics are NOT adopters (their rules differ semantically)
// — this module is scoped to the document/detail-style "row title" concept only.

import type { ColumnDef, Row, SheetMeta } from '../api/types';
import type { ResolvedMediaValue } from '../media/resolveMediaValue';

export interface ResolveTitleColumnOptions {
  /** A per-view override (e.g. DocumentView's own Title select). Wins over
   *  the sheet-level default when it names a column that still exists. */
  overrideColumnId?: string | null;
  /** The grid's current column order (NAMES), ideally visible-only. Used only
   *  when neither an override nor the sheet default resolves. */
  columnOrder?: readonly string[] | null;
}

/** Resolve which column a sheet's rows should show as their "title". */
export function resolveTitleColumn(
  sheet: SheetMeta,
  options?: ResolveTitleColumnOptions,
): ColumnDef | null {
  const byId = (id: string | null | undefined): ColumnDef | null =>
    id != null ? sheet.columns.find((column) => String(column.id) === id) ?? null : null;
  const byName = (name: string): ColumnDef | null =>
    sheet.columns.find((column) => column.name === name) ?? null;

  const override = byId(options?.overrideColumnId);
  if (override) return override;

  const sheetDefault = byId(sheet.titleColumnId);
  if (sheetDefault) return sheetDefault;

  for (const name of options?.columnOrder ?? []) {
    const column = byName(name);
    if (column) return column;
  }

  return sheet.columns[0] ?? null;
}

export interface RowTitleOptions extends ResolveTitleColumnOptions {
  /** Resolved media for this row (document/gallery surfaces) — used as the
   *  fallback label when the title column's value is blank/JSON/missing. */
  media?: ResolvedMediaValue | null;
}

/** Compose a row's display title: the resolved title column's cell value
 *  (skipping blank values and raw JSON objects — DocumentView's JSON-guard),
 *  falling back to the row's media filename/label, falling back to `Row N`. */
export function rowTitle(sheet: SheetMeta, row: Row, options?: RowTitleOptions): string {
  const titleColumn = resolveTitleColumn(sheet, options);
  const raw = titleColumn ? row.cells[String(titleColumn.id)] : null;
  if (raw !== null && raw !== undefined && String(raw).trim() !== '' && !String(raw).startsWith('{')) {
    return String(raw);
  }
  const media = options?.media ?? null;
  return media?.filename ?? media?.label ?? `Row ${row.index + 1}`;
}
