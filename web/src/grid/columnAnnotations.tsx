// Reusable column-annotation affordance.
//
// A header-band chip that annotates a SET of columns (keyed by column id) with
// arbitrary message content and an optional slim progress bar. The first case
// is the run-created "New columns" annotation, but the contract is deliberately
// generic so future callers ride the same surface (e.g. "imported the videos
// but hid some useless columns"): pass a ColumnAnnotation with any `message`
// node and the columns it spans, and the grid positions + renders it identically.
//
// Positioning geometry (the column x-offsets, scroll translation) belongs to
// the grid surface that owns rightEdgeByName/bandX — the same source the
// revive-chevron affordances use — so this component is purely
// presentational: it takes a resolved { left, width } placement and the
// annotation, and draws the chip. That keeps it reusable outside the grid too.

import type { CSSProperties, ReactNode } from 'react';

export interface ColumnAnnotationProgress {
  completed: number;
  total: number;
}

export interface ColumnAnnotation {
  /** Stable identity: React key, scroll-once dedupe target, and dismiss key. */
  key: string;
  /** The columns this annotation spans, by id. The chip is positioned over the
   *  combined header extent of whichever of these are currently visible. */
  columnIds: string[];
  /** Arbitrary message content — 'New columns' today; any node tomorrow. */
  message: ReactNode;
  /** A slim progress bar (completed/total). Omit/null while not populating. */
  progress?: ColumnAnnotationProgress | null;
  /** When provided, a dismiss control renders and calls this on click. */
  onDismiss?: () => void;
  /** Visual tone; defaults to 'info'. */
  tone?: 'info' | 'success';
}

function annotationProgressPct(progress: ColumnAnnotationProgress | null | undefined): number | null {
  if (!progress || progress.total <= 0) return null;
  return Math.max(0, Math.min(100, (100 * progress.completed) / progress.total));
}

// Replay preserve+surface pure surface logic lives in its own fast-refresh-safe
// module ./replayPending (the repo idiom: split non-component exports out of a
// component .tsx). Product code imports it from there directly; this thin
// re-export exists only so the frozen surface check can read the vocabulary
// through the columnAnnotations idiom it rides. The re-export is the one
// non-component export in this component file, so react-refresh is scoped-off
// for just this block.
/* eslint-disable react-refresh/only-export-components */
export {
  advanceAfterAction,
  buildReplayPendingAnnotation,
  firstPendingRowId,
  nextPendingRowId,
  replayPendingAnnotationKey,
  replayPendingChipLabel,
} from './replayPending';
/* eslint-enable react-refresh/only-export-components */

/** Presentational chip. `left`/`minWidth` are already scroll-translated pixel
 *  offsets within the header overlay band (the caller applies bandX). */
export function ColumnAnnotationChip({
  annotation,
  left,
  minWidth,
  headerOffset = 0,
}: {
  annotation: ColumnAnnotation;
  left: number;
  minWidth: number;
  headerOffset?: number;
}) {
  const pct = annotationProgressPct(annotation.progress);
  const style = {
    left,
    minWidth,
    '--grid-column-annotation-header-offset': `${headerOffset}px`,
  } as CSSProperties;
  return (
    <output
      className="grid-column-annotation"
      data-testid="grid-column-annotation"
      data-annotation-key={annotation.key}
      data-column-ids={annotation.columnIds.join(',')}
      data-dismissible={annotation.onDismiss ? 'true' : 'false'}
      data-header-offset={headerOffset}
      // This is a header-overlay chip, not a StatusChip consumer (StatusChip
      // scopes its tone->color vocabulary to its own selector only); its own
      // two-tone border/text swap uses a differently-named attribute so it
      // never collides with that primitive.
      data-annotation-tone={annotation.tone ?? 'info'}
      data-progress={
        annotation.progress ? `${annotation.progress.completed}/${annotation.progress.total}` : ''
      }
      style={style}
    >
      <span className="grid-column-annotation-body">
        <span className="grid-column-annotation-message">{annotation.message}</span>
        {annotation.onDismiss && (
          <button
            type="button"
            className="grid-column-annotation-dismiss"
            data-testid="grid-column-annotation-dismiss"
            title="Dismiss"
            aria-label="Dismiss annotation"
            onClick={annotation.onDismiss}
          >
            <span aria-hidden>×</span>
          </button>
        )}
      </span>
      {pct !== null && (
        // Native <progress> replaces the old role="progressbar" span + a
        // manually-positioned fill child: value/max give the browser real
        // progressbar semantics for free, so the aria-value* attributes the
        // span used to carry by hand are gone too. Unlike WorkbenchBottomDock's
        // progress bar (it also has an indeterminate sliding-animation mode a
        // native <progress> cannot host), this bar is ALWAYS determinate, so
        // the vendor-prefixed pseudo-element restyle in styles.css is a clean
        // fit.
        <progress
          className="grid-column-annotation-progress"
          data-testid="grid-column-annotation-progress"
          value={pct}
          max={100}
        />
      )}
    </output>
  );
}
