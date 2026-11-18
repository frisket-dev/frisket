import { toWireJsonObject } from '../api/viewsLenses';
import { normalizeGridFilterSpec } from './gridColumnState';
import type { GridFilterSpec, GridSortSpec, SheetMeta, ViewSelection, ViewDefinitionInput, ViewPresentation, ViewColumnGroup } from '../api/types';

/** Resolve display names at the browser boundary; saved definitions use stable IDs. */
export function selectionFromGrid(sheet: SheetMeta, filter: GridFilterSpec | null, sort: GridSortSpec | null, rowIds: number[] | null, base?: ViewSelection | null, bindings?: { filterColumnIds?: Record<string, number>; sortColumnIds?: Record<string, number> }): ViewSelection {
  const columnId = (name: string) => {
    const column = sheet.columns.find((item) => item.name === name);
    if (!column) throw new Error(`Column “${name}” is unavailable.`);
    return Number(column.id);
  };
  return {
    ...base,
    scope: { kind: 'sheet', sheet_id: Number(sheet.id), ...(rowIds !== null ? { row_ids: rowIds } : {}), ...(base?.scope.kind === 'sheet' && base.scope.parent_row_id ? { parent_row_id: base.scope.parent_row_id } : {}) },
    filters: Object.entries(filter ?? {}).map(([name, condition]) => ({ column_id: bindings?.filterColumnIds?.[name] ?? columnId(name), condition: toWireJsonObject(condition) })),
    order: sort?.map((item) => ({ column_id: bindings?.sortColumnIds?.[item.column] ?? columnId(item.column), dir: item.dir })) ?? base?.order.filter((item) => 'score' in item) ?? [],
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

export function sameViewDefinition(a: ViewDefinitionInput, b: ViewDefinitionInput): boolean {
  const canonical = (value: unknown): string => {
    if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
    if (value !== null && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => `${JSON.stringify(key)}:${canonical(child)}`).join(',')}}`;
    return JSON.stringify(value);
  };
  return canonical(a) === canonical(b);
}

/** Layout references are IDs on disk; grid-local names are resolved only at the boundary. */
export function presentationFromGrid(sheet: SheetMeta, columns: string[] | null, groups: ViewColumnGroup[]): ViewPresentation {
  const ids = (names: string[]) => names.flatMap(name => {
    const column = sheet.columns.find(item => item.name === name);
    return column ? [Number(column.id)] : [];
  });
  return { columns: columns === null ? null : ids(columns),
    column_groups: groups.length ? groups.map(group => ({ ...group, columns: ids(group.columns) })) : null };
}

export function gridFromPresentation(sheet: SheetMeta, presentation: ViewPresentation): { columns: string[] | null; groups: ViewColumnGroup[] } {
  const names = (ids: number[]) => ids.flatMap(id => {
    const column = sheet.columns.find(item => Number(item.id) === id);
    return column ? [column.name] : [];
  });
  return { columns: presentation.columns == null ? null : names(presentation.columns),
    groups: (presentation.column_groups ?? []).map(group => ({ ...group, columns: names(group.columns) })) };
}
