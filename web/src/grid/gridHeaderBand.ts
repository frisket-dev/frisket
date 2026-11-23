// Pure geometry helpers for SheetGrid's header overlay band (the DOM strip
// that carries the revive chevrons and column annotations over glide's
// canvas header). Kept out of SheetGrid.tsx itself so they're unit-testable
// without mounting the canvas grid — react-refresh also requires a component
// file to export components only, so a shared constant/function needs its
// own module regardless.

interface AnnotationPlacementColumn {
  id?: string;
  group?: string;
}

interface ResolvedAnnotationPlacement {
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
