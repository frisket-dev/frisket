// The floating "+" add-column button (SheetGrid.tsx's header overlay band)
// is centred on `bandX(columnsEndX) + ADD_COLUMN_BUTTON_GAP`, then clamped
// to stay within the visible host — which, on a sheet wide enough to need
// horizontal scrolling, forced the button onto the last column's own "..."
// menu dots once scrolled to the true end. addColumnScrollGutter is the
// pure decision behind the fix: reserve extra glide `overscrollX` scroll
// room, but ONLY when the columns can actually overflow the host, so a
// sheet already narrower than the viewport never gains a phantom scrollbar.

import { describe, expect, it } from 'vitest';
import {
  ADD_COLUMN_BUTTON_GUTTER,
  addColumnScrollGutter,
  resolveColumnAnnotationPlacement,
} from '../../src/grid/gridHeaderBand';

describe('addColumnScrollGutter', () => {
  it('reserves no gutter when there is no add-column affordance', () => {
    expect(addColumnScrollGutter(false, 400, 2000)).toBe(0);
  });

  it('reserves no gutter before the host width has been measured', () => {
    expect(addColumnScrollGutter(true, 0, 2000)).toBe(0);
  });

  it('reserves no gutter for a sheet already narrower than the viewport — no phantom scroll', () => {
    // Columns end well inside the host even with the gutter added on top —
    // nothing here needs to scroll, so overscrollX must stay 0.
    expect(addColumnScrollGutter(true, 1200, 500)).toBe(0);
    // Right at the boundary (content + gutter exactly fills the host): still
    // not overflowing, so still no gutter.
    expect(addColumnScrollGutter(true, 1200, 1200 - ADD_COLUMN_BUTTON_GUTTER)).toBe(0);
  });

  it('reserves the full gutter once the columns can overflow the host', () => {
    // A wide sheet: summed column widths alone already exceed the host.
    expect(addColumnScrollGutter(true, 900, 2400)).toBe(ADD_COLUMN_BUTTON_GUTTER);
    // Right past the boundary: content fits today, but not once the
    // button's own footprint is accounted for — still reserve it, since
    // that's precisely the case that used to clamp the button onto the
    // last column's header.
    expect(addColumnScrollGutter(true, 1200, 1200 - ADD_COLUMN_BUTTON_GUTTER + 1)).toBe(
      ADD_COLUMN_BUTTON_GUTTER,
    );
  });

  it('honors a caller-supplied gutter width instead of the default', () => {
    expect(addColumnScrollGutter(true, 500, 900, 30)).toBe(30);
    expect(addColumnScrollGutter(true, 500, 200, 30)).toBe(0);
  });
});

describe('resolveColumnAnnotationPlacement', () => {
  const columns = [
    { id: 'source', group: undefined },
    { id: 'answer', group: 'ai-run:7' },
    { id: 'confidence', group: 'ai-run:7' },
    { id: 'notes', group: undefined },
  ];

  it('anchors a same-group annotation to the full group and above its header row', () => {
    expect(resolveColumnAnnotationPlacement(['confidence'], columns, 28)).toEqual({
      first: 1,
      last: 2,
      headerOffset: 28,
    });
  });

  it('keeps ordinary column geometry for ungrouped and mixed-group spans', () => {
    expect(resolveColumnAnnotationPlacement(['source'], columns, 28)).toEqual({
      first: 0,
      last: 0,
      headerOffset: 0,
    });
    expect(resolveColumnAnnotationPlacement(['source', 'answer'], columns, 28)).toEqual({
      first: 0,
      last: 1,
      headerOffset: 0,
    });
  });

  it('ignores hidden annotation members when resolving the visible group', () => {
    expect(resolveColumnAnnotationPlacement(['answer', 'hidden-column'], columns, 28)).toEqual({
      first: 1,
      last: 2,
      headerOffset: 28,
    });
  });
});
