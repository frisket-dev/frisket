// Pure geometry helpers for SheetGrid's header overlay band (the DOM strip
// that carries the revive chevrons and the add-column "+" over glide's
// canvas header). Kept out of SheetGrid.tsx itself so they're unit-testable
// without mounting the canvas grid — react-refresh also requires a component
// file to export components only, so a shared constant/function needs its
// own module regardless.

// Reserved scroll gutter (glide's `overscrollX`) so the "+" button's own
// footprint (the gap that clears it from the last column's edge, plus
// roughly its own centred width) never has to land ON the last column's
// header. Without it, scrolling a wide sheet to its true end left nowhere
// for the button but on top of the last column's own "..." menu dots —
// SheetGrid.tsx's `left` clamp (`Math.min(..., hostWidth - 20)`) forced it
// there. Only applied when the columns actually overflow the host (see
// addColumnScrollGutter) — a sheet already narrower than the viewport has
// plenty of empty room after its last column and must not gain a phantom
// scrollbar from this.
export const ADD_COLUMN_BUTTON_GUTTER = 64;

export interface AnnotationPlacementColumn {
  id?: string;
  group?: string;
}

export interface ResolvedAnnotationPlacement {
  first: number;
  last: number;
  headerOffset: number;
}

/**
 * Resolve an annotation's visible column span and which header row it belongs
 * to. A same-group annotation describes the group, so it spans that group's
 * full visible extent and clears the native group-header row. Ungrouped and
 * mixed-group annotations keep their ordinary column-header geometry.
 */
export function resolveColumnAnnotationPlacement(
  annotationColumnIds: readonly string[],
  columns: readonly AnnotationPlacementColumn[],
  groupHeaderHeight: number,
): ResolvedAnnotationPlacement | null {
  const wanted = new Set(annotationColumnIds);
  const annotatedIndices = columns.flatMap((column, index) => (
    column.id !== undefined && wanted.has(column.id) ? [index] : []
  ));
  if (annotatedIndices.length === 0) return null;

  let first = Math.min(...annotatedIndices);
  let last = Math.max(...annotatedIndices);
  const group = columns[first]?.group;
  const sharesGroup = group !== undefined && annotatedIndices.every(
    (index) => columns[index]?.group === group,
  );
  if (sharesGroup) {
    const groupIndices = columns.flatMap((column, index) => (
      column.group === group ? [index] : []
    ));
    first = Math.min(...groupIndices);
    last = Math.max(...groupIndices);
  }

  return {
    first,
    last,
    headerOffset: sharesGroup ? groupHeaderHeight : 0,
  };
}

/**
 * Pure decision for the header overlay band's `overscrollX`: reserve
 * `gutter` px of extra glide scroll room only when the columns can actually
 * overflow the host — the one condition under which scrolling to the true
 * end could otherwise land the floating "+" button on the last column's own
 * header controls. `columnsEndX` and `hostWidth` share the same coordinate
 * space SheetGrid already uses for `bandX` (row-marker width + summed
 * column widths vs the host's clientWidth), so this mirrors glide's own
 * "does this content need to scroll" comparison.
 */
export function addColumnScrollGutter(
  hasAddColumn: boolean,
  hostWidth: number,
  columnsEndX: number,
  gutter: number = ADD_COLUMN_BUTTON_GUTTER,
): number {
  if (!hasAddColumn || hostWidth <= 0) return 0;
  return columnsEndX + gutter > hostWidth ? gutter : 0;
}
