/**
 * The presentational shell shared by the `useResizable` (./useResizable)
 * drag/keyboard resize seams. The interesting a11y LOGIC (pointer drag,
 * keyboard step, clamping, localStorage persistence) lives in one place,
 * `useResizable`.
 *
 * Stays a `<div role="separator">`, not `<hr>`: the seam is keyboard-operable
 * (tabIndex + onKeyDown). Converting to `<hr>` trades `prefer-tag-over-role` for
 * `no-noninteractive-element-interactions` + `no-noninteractive-tabindex` — `<hr>`
 * is non-interactive semantics, incompatible with a focusable/keyboard resize
 * control.
 *
 * `aria-valuenow/valuemin/valuemax` expose the current split position — with four
 * separate copies this was a four-site edit that could drift; with one component
 * it is added once and applies everywhere.
 */
export interface ResizeSeamProps {
  /** Full className for the seam element, including any per-site resizing
   *  modifier the caller wants applied (sites differ on whether the modifier
   *  lands on the seam itself or a sibling — passed through as-is to keep
   *  each site's existing visual behavior byte-identical). */
  className: string;
  testId: string;
  ariaLabel: string;
  /** Current resized dimension (`useResizable`'s `width`) — becomes
   *  `aria-valuenow`. */
  width: number;
  /** The `useResizable` options' `minWidth`/`maxWidth` — become
   *  `aria-valuemin`/`aria-valuemax`. */
  min: number;
  max: number;
  orientation?: 'vertical' | 'horizontal';
  onResizeStart: React.PointerEventHandler<HTMLDivElement>;
  onResizeKeyDown: React.KeyboardEventHandler<HTMLDivElement>;
}

export function ResizeSeam({
  className,
  testId,
  ariaLabel,
  width,
  min,
  max,
  orientation = 'vertical',
  onResizeStart,
  onResizeKeyDown,
}: ResizeSeamProps) {
  return (
    <div
      className={className}
      data-testid={testId}
      role="separator"
      aria-orientation={orientation}
      aria-label={ariaLabel}
      aria-valuenow={width}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      onPointerDown={onResizeStart}
      onKeyDown={onResizeKeyDown}
    />
  );
}
