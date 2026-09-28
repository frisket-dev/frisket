import { ChevronsRight } from 'lucide-react';
import {
  Fragment,
  useEffect,
  useRef,
  useState,
  type ReactNode,
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
  /** The label for the visible tablist. Omit for a row of ordinary buttons. */
  tabListLabel?: string;
  /** Renders live markup or an inert, natural-width measurement copy. */
  renderItem(item: T, measuring: boolean): ReactNode;
  /** Owns the menu item's content, click behavior, and accessible role. */
  renderOverflowItem(item: T, closeMenu: () => void): ReactNode;
  className?: string;
  triggerClassName?: string;
  menuClassName?: string;
  triggerTestId: string;
  menuTestId: string;
  overflowLabel: string;
  overflowTitle?: string;
  menuAriaLabel?: string;
  menuWidth: number;
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
  tabListLabel,
  renderItem,
  renderOverflowItem,
  className,
  triggerClassName,
  menuClassName,
  triggerTestId,
  menuTestId,
  overflowLabel,
  overflowTitle,
  menuAriaLabel,
  menuWidth,
  menuGap = 4,
}: OverflowRowProps<T>) {
  const triggerMeasureRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const { rowRef, measureRef, visibleItems, overflowItems } = useOverflowItems(items, {
    getKey,
    keepVisibleKey,
    overflowControlRef: triggerMeasureRef,
  });

  useEffect(() => {
    if (overflowItems.length === 0) setMenuOpen(false);
  }, [overflowItems.length]);

  useNativePopover(menuRef, () => setMenuOpen(false), {
    enabled: menuOpen,
    focusRestore: true,
    ignoreSelector: `[data-testid="${triggerTestId}"]`,
  });
  const menuPosition = useAnchoredPosition(triggerRef, {
    enabled: menuOpen,
    align: 'right',
    width: menuWidth,
    gap: menuGap,
  });

  const rowClassName = className ? `${styles.row} ${className}` : styles.row;
  const triggerClasses = triggerClassName ? `${styles.trigger} ${triggerClassName}` : styles.trigger;
  const menuClasses = menuClassName ? menuClassName : undefined;

  return (
    <div ref={rowRef} className={rowClassName}>
      <div className={styles.items} role={tabListLabel ? 'tablist' : undefined} aria-label={tabListLabel}>
        {visibleItems.map((item) => <div className={styles.item} key={getKey(item)}>{renderItem(item, false)}</div>)}
      </div>
      {overflowItems.length > 0 && (
        <>
          <button
            ref={triggerRef}
            type="button"
            className={triggerClasses}
            data-testid={triggerTestId}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-label={overflowLabel}
            title={overflowTitle ?? overflowLabel}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <ChevronsRight size={16} aria-hidden />
          </button>
          {menuOpen && (
            <MenuPop
              ref={menuRef}
              className={menuClasses}
              data-testid={menuTestId}
              aria-label={menuAriaLabel}
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
                  {renderOverflowItem(item, () => setMenuOpen(false))}
                </Fragment>
              ))}
            </MenuPop>
          )}
        </>
      )}
      <div ref={measureRef} className={styles.measure} aria-hidden="true" inert>
        {items.map((item) => <div className={styles.item} key={getKey(item)}>{renderItem(item, true)}</div>)}
      </div>
      <button
        ref={triggerMeasureRef}
        type="button"
        className={triggerClassName ? `${styles.triggerMeasure} ${triggerClassName}` : styles.triggerMeasure}
        aria-hidden="true"
        tabIndex={-1}
        inert
      >
        <ChevronsRight size={16} aria-hidden />
      </button>
    </div>
  );
}
