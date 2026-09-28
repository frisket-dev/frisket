import { ChevronsRight } from 'lucide-react';
import {
  Fragment,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
  type AriaRole,
} from 'react';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import { useNativePopover } from '../hooks/useNativePopover';
import { MenuPop } from './MenuPop';
import styles from './OverflowRow.module.css';
import { useOverflowItems } from './useOverflowItems';

export interface OverflowRowProps<T> {
  items: readonly T[];
  getKey(item: T): string;
  keepVisibleKey?: string;
  /** Semantics of the visible items; omitted for ordinary layout rows. */
  role?: AriaRole;
  'aria-label'?: string;
  /** Renders live markup or an inert, natural-width measurement copy. */
  renderItem(item: T, measuring: boolean): ReactNode;
  /** Owns the menu item's content, click behavior, and accessible role. */
  renderOverflowItem(item: T, closeMenu: () => void): ReactNode;
  className?: string;
  triggerClassName?: string;
  menuClassName?: string;
  triggerTestId?: string;
  menuTestId?: string;
  overflowLabel: string;
  menuWidth?: number;
  menuGap?: number;
}

/**
 * A measured, no-scroll row that promotes overflowed items into a top-layer
 * menu. Callers own item markup so the hidden measurer has the exact same
 * typography, dividers, and padding as the live row.
 */
export function OverflowRow<T>({
  items,
  getKey,
  keepVisibleKey,
  role,
  'aria-label': label,
  renderItem,
  renderOverflowItem,
  className,
  triggerClassName,
  menuClassName,
  triggerTestId,
  menuTestId,
  overflowLabel,
  menuWidth = 220,
  menuGap = 4,
}: OverflowRowProps<T>) {
  const triggerId = useId();
  const triggerMeasureRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const wasOpen = useRef(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const { rowRef, measureRef, visibleItems, overflowItems } = useOverflowItems(items, {
    getKey,
    keepVisibleKey,
    overflowControlRef: triggerMeasureRef,
  });

  if (menuOpen && overflowItems.length === 0) setMenuOpen(false);

  useNativePopover(menuRef, () => setMenuOpen(false), {
    enabled: menuOpen,
    focusRestore: true,
    ignoreSelector: `[id="${triggerId}"]`,
  });
  const menuPosition = useAnchoredPosition(triggerRef, {
    enabled: menuOpen,
    align: 'right',
    width: menuWidth,
    gap: menuGap,
  });

  useEffect(() => {
    if (menuOpen) menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]:not([disabled])')?.focus();
    else if (wasOpen.current) triggerRef.current?.focus();
    wasOpen.current = menuOpen;
  }, [menuOpen]);

  const closeMenu = () => setMenuOpen(false);

  const rowClassName = className ? `${styles.row} ${className}` : styles.row;
  const triggerClasses = triggerClassName ? `${styles.trigger} ${triggerClassName}` : styles.trigger;

  return (
    <div ref={rowRef} className={rowClassName}>
      <div className={styles.items} role={role} aria-label={label}>
        {visibleItems.map((item) => <div className={styles.item} key={getKey(item)}>{renderItem(item, false)}</div>)}
      </div>
      {overflowItems.length > 0 && (
        <>
          <button
            ref={triggerRef}
            id={triggerId}
            type="button"
            className={triggerClasses}
            data-testid={triggerTestId}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-label={overflowLabel}
            title={overflowLabel}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <ChevronsRight size={16} aria-hidden />
          </button>
          {menuOpen && (
            <MenuPop
              ref={menuRef}
              className={menuClassName}
              data-testid={menuTestId}
              aria-label={overflowLabel}
              onKeyDown={(event) => {
                if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
                const entries = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])'));
                if (!entries.length) return;
                event.preventDefault();
                const current = entries.indexOf(document.activeElement as HTMLElement);
                const next = event.key === 'Home' ? 0 : event.key === 'End' ? entries.length - 1
                  : (current + (event.key === 'ArrowDown' ? 1 : -1) + entries.length) % entries.length;
                entries[next].focus();
              }}
              style={menuPosition
                ? {
                    position: 'fixed', inset: 'auto', top: menuPosition.top, bottom: menuPosition.bottom,
                    left: menuPosition.left, right: 'auto', width: menuPosition.width,
                    maxHeight: menuPosition.maxHeight, margin: 0,
                  }
                : { position: 'fixed', visibility: 'hidden' }}
            >
              {overflowItems.map((item) => (
                <Fragment key={getKey(item)}>
                  {renderOverflowItem(item, closeMenu)}
                </Fragment>
              ))}
            </MenuPop>
          )}
        </>
      )}
      <div ref={measureRef} className={styles.measure} aria-hidden="true" {...{ inert: '' }}>
        {items.map((item) => <div className={styles.item} key={getKey(item)}>{renderItem(item, true)}</div>)}
      </div>
      <button
        ref={triggerMeasureRef}
        type="button"
        className={triggerClassName ? `${styles.triggerMeasure} ${triggerClassName}` : styles.triggerMeasure}
        aria-hidden="true"
        tabIndex={-1}
        {...{ inert: '' }}
      >
        <ChevronsRight size={16} aria-hidden />
      </button>
    </div>
  );
}
