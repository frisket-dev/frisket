import './MenuPop.css';
import { forwardRef, useLayoutEffect, useRef, useState, type HTMLAttributes, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { useAnchoredPosition, type AnchoredPositionOptions } from '../hooks/useAnchoredPosition';
import { useNativePopover } from '../hooks/useNativePopover';

export interface MenuPopProps extends HTMLAttributes<HTMLDivElement> {
  /** Extra classes appended after the shared `.menu-pop` visual class. */
  className?: string;
  /** Positioned menus use the anchor's measured width, not the default menu cap. */
  sizing?: 'content' | 'anchored';
}

/**
 * The one wrapper for the `.menu-pop` floating-menu surface. The visual class
 * was unified long ago (styles.css `.menu-pop`); this owns the markup so every
 * popover shares one element contract (`role="menu"` default) instead of each
 * site re-typing `className="menu-pop …"`. Pair with `useNativePopover`
 * (hooks/useNativePopover.ts) on this component's own `ref` for the top-layer
 * dismissal half — pass `ref={popoverRef}` through, same as any native
 * `popover="manual"` element.
 */
export const MenuPop = forwardRef<HTMLDivElement, MenuPopProps>(function MenuPop(
  { className, role = 'menu', sizing = 'content', children, ...rest },
  ref,
) {
  const cls = className ? `menu-pop ${className}` : 'menu-pop';
  return (
    <div ref={ref} className={cls} role={role} data-menu-sizing={sizing} {...rest}>
      {children}
    </div>
  );
});

/** Shared placement and dismissal for menus opened from a control. */
export function AnchoredMenu({
  anchorRef, menuRef: suppliedRef, onClose, width = 240, align = 'left',
  dismissOnFocusOutside = false, onClickCapture, ...props
}: Omit<MenuPopProps, 'style' | 'sizing'> & {
  anchorRef: RefObject<HTMLElement | null>;
  menuRef?: RefObject<HTMLDivElement>;
  onClose(): void;
  width?: AnchoredPositionOptions['width'];
  align?: 'left' | 'right';
  dismissOnFocusOutside?: boolean;
}) {
  const internalRef = useRef<HTMLDivElement>(null);
  const menuRef = suppliedRef ?? internalRef;
  const [portalRoot, setPortalRoot] = useState<Element | null>(null);
  useLayoutEffect(() => {
    setPortalRoot(anchorRef.current?.closest('dialog') ?? document.body);
  }, [anchorRef]);
  const position = useAnchoredPosition(anchorRef, { enabled: true, width, align, gap: 4 });
  useNativePopover(menuRef, onClose, {
    enabled: portalRoot !== null, sourceRef: anchorRef,
    extraRefs: [anchorRef], focusRestore: anchorRef, focusOutside: dismissOnFocusOutside,
  });
  if (!portalRoot) return null;
  return createPortal(
    <MenuPop {...props} ref={menuRef} sizing="anchored"
      onClickCapture={(event) => {
        // A portal is outside the trigger's fieldset; retain its disabled gate.
        if (anchorRef.current?.matches(':disabled')) {
          event.preventDefault();
          event.stopPropagation();
          onClose();
        } else onClickCapture?.(event);
      }}
      style={{ position: 'fixed', inset: 'auto', margin: 0, ...(position ?? { visibility: 'hidden' }) }} />,
    portalRoot,
  );
}
