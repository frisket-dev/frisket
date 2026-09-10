import { GridCellKind, type GridCell } from '@glideapps/glide-data-grid';
import type { ColumnDef, PreviewOverlayCell, PreviewOverlayColumn, Row } from '../api/types';
import { buildCell, type BuildCellOptions } from './cells';

export function previewColumnDef(column: PreviewOverlayColumn): ColumnDef {
  return {
    id: `__preview::${column.name}`,
    name: column.name,
    type: column.columnType,
    format: column.format,
    defaultHidden: column.hidden,
  };
}

export function previewValueRow(column: ColumnDef, cell: PreviewOverlayCell): Row {
  return {
    id: '__preview',
    index: 0,
    cells: { [column.id]: cell.value },
    provenance: {},
    cellStates: { [column.id]: 'complete' },
  };
}

export function previewGridCell(
  column: PreviewOverlayColumn,
  cell: PreviewOverlayCell | undefined,
  options: BuildCellOptions,
): GridCell & { readonly: true } {
  if (!cell || cell.error) {
    const text = cell?.error ? `⚠ ${cell.error}` : '';
    return {
      kind: GridCellKind.Text,
      data: text,
      displayData: text,
      allowOverlay: false,
      readonly: true,
      allowWrapping: options.wrap,
      ...(cell?.error ? { themeOverride: options.gridCellPalette?.error } : {}),
    };
  }
  const def = previewColumnDef(column);
  return {
    ...buildCell(def, previewValueRow(def, cell), options),
    // Scalar renderers can return editable/interactive cells. A preview never is.
    allowOverlay: false,
    readonly: true,
  };
}
