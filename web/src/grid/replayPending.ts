// Replay preserve+surface. The pure, framework-free surface logic behind the "N
// updated values · Review" column chip and its per-cell accept popover. Kept in
// its own module (not the columnAnnotations .tsx, which exports a component) so
// both the grid wiring and the vitest surface check import plain,
// fast-refresh-safe functions.
//
// The chip rides the existing ColumnAnnotation idiom: the scroll-into-view
// choreography, header positioning, and dismiss affordance are already built by
// SheetGrid — this only supplies the annotation object and the pure
// navigation/label helpers.

import type { ReactNode } from 'react';

import type { ColumnAnnotation } from './columnAnnotations';

/** Plural-aware chip label: "1 updated value" / "12 updated values". */
export function replayPendingChipLabel(count: number): string {
  return `${count} updated value${count === 1 ? '' : 's'}`;
}

/** Stable annotation identity so scroll-into-view fires once per column+run and
 *  a later re-run (new runId) mints a fresh key that re-surfaces. */
export function replayPendingAnnotationKey(columnId: string, runId: number): string {
  return `replay-pending:${columnId}:${runId}`;
}

/** Build the column-level pending chip as a ColumnAnnotation: info tone, no
 *  populating progress bar (the count is static), and a durable onDismiss that
 *  keeps every edit in the column. The caller supplies the
 *  message node (the label + Review / Accept-all controls). */
export function buildReplayPendingAnnotation(opts: {
  columnId: string;
  runId: number;
  message: ReactNode;
  onDismiss: () => void;
}): ColumnAnnotation {
  return {
    key: replayPendingAnnotationKey(opts.columnId, opts.runId),
    columnIds: [opts.columnId],
    message: opts.message,
    tone: 'info',
    onDismiss: opts.onDismiss,
  };
}

/** Review scrolls to the FIRST pending cell in the column. */
export function firstPendingRowId(ordered: number[]): number | null {
  return ordered.length > 0 ? ordered[0] : null;
}

/** Wrapping navigation across pending cells; an unknown current falls back to
 *  the first pending cell. */
export function nextPendingRowId(ordered: number[], current: number): number | null {
  if (ordered.length === 0) return null;
  const index = ordered.indexOf(current);
  if (index < 0) return ordered[0];
  return ordered[(index + 1) % ordered.length];
}

/** After Accept/Keep on `acted`, auto-advance to the next still-pending cell,
 *  wrapping; null once the column is cleared. The acted cell has left the
 *  pending set (accept self-clears; keep is a durable dismiss). */
export function advanceAfterAction(ordered: number[], acted: number): number | null {
  const remaining = ordered.filter((rowId) => rowId !== acted);
  if (remaining.length === 0) return null;
  const index = ordered.indexOf(acted);
  return remaining[(index < 0 ? 0 : index) % remaining.length];
}
