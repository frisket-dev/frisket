// Design-system select for the action panel: the real select stays in the
// DOM as the trigger (so tests' selectOption(), form value semantics, and
// keyboard value-cycling keep working), but pointer users get a custom
// popover with rich option rows — descriptions, AI-column ⚡ markers, and a
// check on the selected value. The popover portals to <body> with fixed
// positioning so the panel's overflow never clips it, opening toward
// whichever side of the trigger has more room.

import {
  forwardRef,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEventHandler,
  type KeyboardEventHandler,
  type MouseEventHandler,
  type ReactNode,
  type RefObject,
  type SelectHTMLAttributes,
} from 'react';
import { createPortal } from 'react-dom';
import { Check, Zap } from 'lucide-react';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';

export interface PanelSelectOption {
  value: string;
  label: string;
  description?: string;
  /** Optional native ``optgroup`` label. */
  group?: string;
  /** Mark an AI-generated column with the ⚡ badge. */
  ai?: boolean;
  disabled?: boolean;
}

interface PanelSelectProps extends Omit<
  SelectHTMLAttributes<HTMLSelectElement>,
  'children' | 'onChange' | 'value'
> {
  value: string | number;
  /** Preferred value-only callback for new call sites. */
  onValueChange?(value: string): void;
  /** Native callback retained for incremental migrations and form adapters. */
  onChange?: ChangeEventHandler<HTMLSelectElement>;
  /** Rich options are preferred; native option/optgroup children are accepted
   *  so existing controlled selects can adopt the design-system surface
   *  without reimplementing their option construction. */
  options?: PanelSelectOption[];
  children?: ReactNode;
  'data-testid'?: string;
  testId?: string;
  ariaLabel?: string;
  /** Non-selectable row shown when ``options`` is empty. */
  emptyMessage?: string;
  /** Muted qualifier shown at the trigger's right edge (e.g. "local, default"),
   *  overlaid on the native select without affecting its value. */
  note?: string;
  /** Promote the custom menu to the browser top layer when this select is
   *  nested inside another top-layer surface. */
  topLayer?: boolean;
  /** Exposes the custom menu for an enclosing popover's inside-click boundary. */
  menuRef?: RefObject<HTMLDivElement>;
}

function nativeOptions(select: HTMLSelectElement | null): PanelSelectOption[] {
  if (!select) return [];
  return Array.from(select.options).map((option) => {
    const group = option.parentElement instanceof HTMLOptGroupElement
      ? option.parentElement
      : undefined;
    return {
      value: option.value,
      label: option.label,
      disabled: option.disabled || group?.disabled,
      group: group?.label,
    };
  });
}

function setNativeSelectValue(select: HTMLSelectElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
  setter?.call(select, value);
  select.dispatchEvent(new Event('change', { bubbles: true }));
}

export const PanelSelect = forwardRef<HTMLSelectElement, PanelSelectProps>(function PanelSelect({
  value,
  onValueChange,
  onChange,
  options: suppliedOptions,
  children,
  className = 'form-input',
  id,
  testId,
  ariaLabel,
  disabled,
  emptyMessage = 'No items found',
  note,
  topLayer = false,
  menuRef: suppliedMenuRef,
  onMouseDown,
  onKeyDown,
  'data-testid': nativeTestId,
  'aria-label': nativeAriaLabel,
  ...selectProps
}, forwardedRef) {
  const [open, setOpen] = useState(false);
  const selectRef = useRef<HTMLSelectElement | null>(null);
  const internalMenuRef = useRef<HTMLDivElement | null>(null);
  const menuRef = suppliedMenuRef ?? internalMenuRef;
  const menuPos = useAnchoredPosition(selectRef, {
    enabled: open,
    width: (rect) => Math.max(rect.width, 200),
    gap: 5,
    minHeight: 180,
  });
  const effectiveTestId = testId ?? nativeTestId;

  useEffect(() => {
    if (!open) return undefined;
    const closeIfOutside = (event: PointerEvent | FocusEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (selectRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener('pointerdown', closeIfOutside);
    document.addEventListener('focusin', closeIfOutside);
    return () => {
      document.removeEventListener('pointerdown', closeIfOutside);
      document.removeEventListener('focusin', closeIfOutside);
    };
  }, [menuRef, open]);

  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!open || !topLayer || !menu) return undefined;
    if (menu.getAttribute('popover') !== 'manual') menu.setAttribute('popover', 'manual');
    if (menu.isConnected && !menu.matches(':popover-open')) menu.showPopover();
    return () => {
      if (menu.isConnected && menu.matches(':popover-open')) menu.hidePopover();
    };
  }, [menuRef, open, topLayer]);

  const options = suppliedOptions ?? nativeOptions(selectRef.current);
  const selectedValue = String(value);

  const choose = (next: string) => {
    if (selectRef.current) setNativeSelectValue(selectRef.current, next);
    else onValueChange?.(next);
    setOpen(false);
    selectRef.current?.focus();
  };

  const setSelectRef = useCallback((node: HTMLSelectElement | null) => {
    selectRef.current = node;
    if (typeof forwardedRef === 'function') forwardedRef(node);
    else if (forwardedRef) forwardedRef.current = node;
  }, [forwardedRef]);

  const handleMouseDown: MouseEventHandler<HTMLSelectElement> = (event) => {
    onMouseDown?.(event);
    if (event.defaultPrevented || disabled) return;
    // Suppress the native dropdown; the popover replaces it.
    event.preventDefault();
    selectRef.current?.focus();
    setOpen((prev) => !prev);
  };

  const handleKeyDown: KeyboardEventHandler<HTMLSelectElement> = (event) => {
    onKeyDown?.(event);
    if (event.defaultPrevented) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      setOpen((prev) => !prev);
    }
    if (event.key === 'Escape') setOpen(false);
  };

  const selectEl = (
    <select data-native-select-escape="panel-select-trigger"
      {...selectProps}
      ref={setSelectRef}
      className={className}
      id={id}
      data-testid={effectiveTestId}
      aria-label={ariaLabel ?? nativeAriaLabel}
      aria-expanded={open}
      disabled={disabled}
      value={value}
      onChange={(event) => {
        onValueChange?.(event.currentTarget.value);
        onChange?.(event);
      }}
      onMouseDown={handleMouseDown}
      onKeyDown={handleKeyDown}
    >
      {children ?? (options.length === 0 ? (
        <option value="" disabled>{emptyMessage}</option>
      ) : (
        options.map((option) => (
          <option key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
            {option.ai ? ' ⚡' : ''}
          </option>
        ))
      ))}
    </select>
  );

  return (
    <>
      {note ? (
        <span className="panel-select-shell">
          {selectEl}
          <span className="panel-select-note" aria-hidden>{note}</span>
        </span>
      ) : (
        selectEl
      )}
      {open && !disabled &&
        createPortal(
          <div
            className="panel-select-menu"
            role="listbox"
            ref={menuRef}
            data-testid={effectiveTestId ? `${effectiveTestId}-menu` : undefined}
            style={
              menuPos
                ? {
                    top: menuPos.top,
                    bottom: menuPos.bottom,
                    left: menuPos.left,
                    width: menuPos.width,
                    maxHeight: menuPos.maxHeight,
                  }
                : { visibility: 'hidden' }
            }
          >
            {options.length === 0 ? (
              <div
                role="option"
                aria-selected="false"
                aria-disabled="true"
                className="panel-select-option is-empty"
              >
                <span className="panel-select-option-main">
                  <span className="panel-select-option-label">{emptyMessage}</span>
                </span>
              </div>
            ) : (
              options.map((option, index) => {
                const showGroup = option.group && option.group !== options[index - 1]?.group;
                return (
                  <div key={`${option.group ?? ''}:${option.value}:${index}`} role="presentation">
                    {showGroup && (
                      <div className="panel-select-group" role="presentation">
                        {option.group}
                      </div>
                    )}
                    <button
                      type="button"
                      role="option"
                      aria-selected={option.value === selectedValue}
                      className={`panel-select-option${option.value === selectedValue ? ' is-selected' : ''}`}
                      disabled={option.disabled}
                      onClick={() => choose(option.value)}
                    >
                      <span className="panel-select-option-main">
                        <span className="panel-select-option-label">
                          {option.ai && <Zap size={12} className="panel-select-ai" aria-label="AI column" />}
                          {option.label}
                        </span>
                        {option.description && (
                          <span className="panel-select-option-desc">{option.description}</span>
                        )}
                      </span>
                      {option.value === selectedValue && <Check size={13} className="panel-select-check" aria-hidden />}
                    </button>
                  </div>
                );
              })
            )}
          </div>,
          document.body,
        )}
    </>
  );
});
