// The ⚙ view-options popover's plumbing, shared by the Document view's two
// readers (media and annotated text). Extracted from DocumentReader.tsx when
// the text reader needed the same contract: a top-layer popover, anchored to
// its own trigger, dismissed by the shared native-popover hook, with the ref
// and placement style handed to a render-prop so the CALLER attaches them via
// ordinary JSX `ref=`/`style=` and neither side writes a ref outside JSX.
import { useCallback, useRef, type CSSProperties, type RefObject } from 'react';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import { useNativePopover } from '../hooks/useNativePopover';

export interface OptionsPopoverPlumbing {
  /** Attach with JSX `ref=` on the ⚙ button. Typed as @types/react's read-only
   *  JSX ref shape (not `| null`) — this hook only ever READS it. */
  triggerRef: RefObject<HTMLButtonElement>;
  attachPopover(el: HTMLDivElement | null): void;
  popoverStyle: CSSProperties;
}

export function useOptionsPopover(
  open: boolean,
  onToggle: () => void,
  /** The trigger's own test id, so a click on the trigger toggles rather than
   *  being eaten as an outside-dismiss. */
  triggerSelector: string,
  /** Menus this popover's options portal to <body> — counted as "inside" so
   *  choosing an option does not read as an outside-dismiss. */
  insideRefs: ReadonlyArray<RefObject<HTMLElement | null>> = [],
): OptionsPopoverPlumbing {
  const triggerRef = useRef<HTMLButtonElement>(null);
  // Explicitly `| null` in the generic (not `useRef<HTMLDivElement>(null)`,
  // which @types/react resolves to a read-only RefObject meant for JSX `ref=`
  // only) so `.current` stays assignable from the callback ref below.
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const attachPopover = useCallback((el: HTMLDivElement | null) => {
    popoverRef.current = el;
  }, []);
  useNativePopover(popoverRef, onToggle, {
    enabled: open,
    ignoreSelector: triggerSelector,
    extraRefs: insideRefs,
  });
  const position = useAnchoredPosition(triggerRef, {
    enabled: open,
    align: 'right',
    width: 232,
    gap: 4,
  });
  const popoverStyle: CSSProperties = position
    ? {
      position: 'fixed',
      inset: 'auto',
      top: position.top,
      bottom: position.bottom,
      left: position.left,
      right: 'auto',
      width: position.width,
      margin: 0,
    }
    : { position: 'fixed', visibility: 'hidden' };
  return { triggerRef, attachPopover, popoverStyle };
}
