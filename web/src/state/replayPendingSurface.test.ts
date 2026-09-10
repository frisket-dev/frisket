// Frozen red-first checks for the replay-accept grid surface. v1 is deliberately
// simple: preserve the human edit, surface the fresh generated value, allow
// accept. The rich prev/current/potential fast-scan compare is DEFERRED.
//
// The surface rides the existing columnAnnotations idiom: a
// header-band chip keyed by columnIds carrying a "{n} updated values · Review"
// message plus Accept-all, with the built-in scroll-into-view choreography.
// This exercises the pure, framework-free surface logic that the SheetGrid
// wiring builds on:
//
//   - replayPendingChipLabel(n)          -> "12 updated values" (plural aware)
//   - replayPendingAnnotationKey(col,run)-> stable "replay-pending:<col>:<run>"
//   - buildReplayPendingAnnotation(...)  -> a ColumnAnnotation (info tone,
//                                           durable onDismiss = keep column)
//   - firstPendingRowId(ordered)         -> Review scrolls to first pending
//   - nextPendingRowId(ordered, current) -> wrapping navigation
//   - advanceAfterAction(ordered, acted) -> auto-advance to the next pending
//                                           after Accept/Keep
//
// These import the EXISTING grid module so the pre-build red is a clean Vitest
// `expect` assertion failure (the new exports are absent), never an unresolved
// import / collection error.

import { describe, expect, it, vi } from 'vitest';

import * as annotations from '../grid/columnAnnotations';

type ReplaySurface = {
  replayPendingChipLabel: (count: number) => string;
  replayPendingAnnotationKey: (columnId: string, runId: number) => string;
  buildReplayPendingAnnotation: (opts: {
    columnId: string;
    runId: number;
    message: unknown;
    onDismiss: () => void;
  }) => {
    key: string;
    columnIds: string[];
    tone?: string;
    message: unknown;
    onDismiss?: () => void;
    progress?: unknown;
  };
  firstPendingRowId: (ordered: number[]) => number | null;
  nextPendingRowId: (ordered: number[], current: number) => number | null;
  advanceAfterAction: (ordered: number[], acted: number) => number | null;
};

const surface = annotations as unknown as ReplaySurface;

describe('replay-accept surface (columnAnnotations idiom)', () => {
  it('labels the chip with a plural-aware updated-values count', () => {
    expect(typeof surface.replayPendingChipLabel).toBe('function');
    expect(surface.replayPendingChipLabel(12)).toBe('12 updated values');
    expect(surface.replayPendingChipLabel(1)).toBe('1 updated value');
  });

  it('keys the annotation stably per column+run so scroll fires once', () => {
    expect(typeof surface.replayPendingAnnotationKey).toBe('function');
    // Stable identity: scroll-into-view fires once per key; a later
    // re-run (new runId) mints a fresh key and re-surfaces.
    expect(surface.replayPendingAnnotationKey('42', 7)).toBe('replay-pending:42:7');
    expect(surface.replayPendingAnnotationKey('42', 8)).not.toBe(
      surface.replayPendingAnnotationKey('42', 7),
    );
  });

  it('builds an info-tone ColumnAnnotation spanning the one column with a durable dismiss', () => {
    expect(typeof surface.buildReplayPendingAnnotation).toBe('function');
    const onDismiss = vi.fn();
    const message = { marker: 'chip-body' };
    const annotation = surface.buildReplayPendingAnnotation({
      columnId: '42',
      runId: 7,
      message,
      onDismiss,
    });
    expect(annotation.key).toBe('replay-pending:42:7');
    expect(annotation.columnIds).toEqual(['42']);
    expect(annotation.tone).toBe('info');
    expect(annotation.message).toBe(message);
    // no populating progress bar — the count is static
    expect(annotation.progress ?? null).toBeNull();
    // durable "keep every edit in this column" dismiss
    expect(typeof annotation.onDismiss).toBe('function');
    annotation.onDismiss?.();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('Review scrolls to the FIRST pending cell in the column (dispositions #1)', () => {
    expect(typeof surface.firstPendingRowId).toBe('function');
    expect(surface.firstPendingRowId([31, 12, 88])).toBe(31);
    expect(surface.firstPendingRowId([])).toBeNull();
  });

  it('navigates pending cells with wrapping (dispositions #1)', () => {
    expect(typeof surface.nextPendingRowId).toBe('function');
    expect(surface.nextPendingRowId([31, 12, 88], 31)).toBe(12);
    expect(surface.nextPendingRowId([31, 12, 88], 88)).toBe(31); // wraps
    // an unknown current falls back to the first pending cell
    expect(surface.nextPendingRowId([31, 12, 88], 999)).toBe(31);
  });

  it('auto-advances to the next remaining pending cell after Accept/Keep (dispositions #1)', () => {
    expect(typeof surface.advanceAfterAction).toBe('function');
    // acting on the middle cell advances to the next one, which is still pending
    expect(surface.advanceAfterAction([31, 12, 88], 12)).toBe(88);
    // acting on the last cell wraps to the first remaining
    expect(surface.advanceAfterAction([31, 12, 88], 88)).toBe(31);
    // acting on the only remaining cell clears the popover
    expect(surface.advanceAfterAction([31], 31)).toBeNull();
  });
});
