import { toWireJsonObject } from '../api/viewsLenses';
import { normalizeGridFilterSpec } from './gridColumnState';
import type { GridFilterSpec, GridSortSpec, SheetMeta, ViewSelection, SavedViewDefinitionInput } from '../api/types';

/** Resolve display names at the browser boundary; saved definitions use stable IDs. */
export function selectionFromGrid(sheet: SheetMeta, filter: GridFilterSpec | null, sort: GridSortSpec | null, rowIds: number[] | null, base?: ViewSelection | null): ViewSelection {
  const columnId = (name: string) => {
    const column = sheet.columns.find((item) => item.name === name);
    if (!column) throw new Error(`Column “${name}” is unavailable.`);
    return Number(column.id);
  };
  return {
    ...base,
    scope: { kind: 'sheet', sheet_id: Number(sheet.id), ...(rowIds !== null ? { row_ids: rowIds } : {}), ...(base?.scope.kind === 'sheet' && base.scope.parent_row_id ? { parent_row_id: base.scope.parent_row_id } : {}) },
    filters: Object.entries(filter ?? {}).map(([name, condition]) => ({ column_id: columnId(name), condition: toWireJsonObject(condition) })),
    order: sort?.map((item) => ({ column_id: columnId(item.column), dir: item.dir })) ?? base?.order.filter((item) => 'score' in item) ?? [],
    limit: base?.limit ?? null,
  };
}

export function gridFromSelection(sheet: SheetMeta, selection: ViewSelection): { filter: GridFilterSpec; sort: GridSortSpec } {
  const name = (id: number) => {
    const column = sheet.columns.find((item) => Number(item.id) === id);
    if (!column) throw new Error(`View column ${id} is unavailable.`);
    return column.name;
  };
  return {
    filter: normalizeGridFilterSpec(Object.fromEntries(selection.filters.map((item) => [name(item.column_id), item.condition]))) ?? {},
    sort: selection.order.flatMap((item) => 'column_id' in item ? [{ column: name(item.column_id), dir: item.dir }] : []),
  };
}

export function sameViewDefinition(a: SavedViewDefinitionInput, b: SavedViewDefinitionInput): boolean {
  const canonical = (value: unknown): string => {
    if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
    if (value !== null && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => `${JSON.stringify(key)}:${canonical(child)}`).join(',')}}`;
    return JSON.stringify(value);
  };
  return canonical(a) === canonical(b);
}
