import { forwardRef, type HTMLAttributes } from 'react';

export interface MenuPopProps extends HTMLAttributes<HTMLDivElement> {
  /** Extra classes appended after the shared `.menu-pop` visual class. */
  className?: string;
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
  { className, role = 'menu', children, ...rest },
  ref,
) {
  const cls = className ? `menu-pop ${className}` : 'menu-pop';
  return (
    <div ref={ref} className={cls} role={role} {...rest}>
      {children}
    </div>
  );
});
