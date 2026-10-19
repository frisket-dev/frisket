// Design-system select for the action panel: the real select stays in the
// DOM as the trigger (so tests' selectOption(), form value semantics, and
// keyboard value-cycling keep working), but pointer users get a custom
// popover with rich option rows — descriptions, AI-column ⚡ markers, and a
// check on the selected value. The popover portals to its owning dialog (or
// <body> outside a dialog) with fixed
// positioning so the panel's overflow never clips it, opening toward
// whichever side of the trigger has more room.

import {
  forwardRef,
  useCallback,
  useRef,
  useState,
  type ChangeEventHandler,
  type KeyboardEventHandler,
  type MouseEventHandler,
  type ReactNode,
  type RefObject,
  type SelectHTMLAttributes,
} from 'react';
import { Check, Zap } from 'lucide-react';
import { DropdownIndicator } from './DropdownIndicator';
import { AnchoredMenu } from './MenuPop';
import './PanelSelect.css';

export interface PanelSelectOption {
  value: string;
  label: string;
  description?: string;
  /** Optional visual cue shown before this option's copy. */
  leadingIcon?: ReactNode;
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
  className = '',
  id,
  testId,
  ariaLabel,
  disabled,
  emptyMessage = 'No items found',
  note,
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
  const effectiveTestId = testId ?? nativeTestId;

  const options = suppliedOptions ?? nativeOptions(selectRef.current);
  const selectedValue = String(value);
  const leadingIcon = options.find((option) => option.value === selectedValue)?.leadingIcon;

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
      className={`panel-select-control select-trigger form-input ${className}`}
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
      {!selectProps.multiple ? (
        <span className={`panel-select-shell${leadingIcon ? ' has-leading-icon' : ''}`}>
          {selectEl}
          {leadingIcon && <span className="panel-select-leading-icon">{leadingIcon}</span>}
          {note && <span className="panel-select-note" aria-hidden>{note}</span>}
          <DropdownIndicator />
        </span>
      ) : (
        selectEl
      )}
      {open && !disabled &&
          <AnchoredMenu
            className="select-menu"
            role="listbox"
            menuRef={menuRef}
            anchorRef={selectRef}
            onClose={() => setOpen(false)}
            width={(rect) => Math.max(rect.width, 200)}
            dismissOnFocusOutside
            data-testid={effectiveTestId ? `${effectiveTestId}-menu` : undefined}
          >
            {options.length === 0 ? (
              <div
                role="option"
                aria-selected="false"
                aria-disabled="true"
                className="menu-empty"
              >
                {emptyMessage}
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
                      className="menu-item"
                      disabled={option.disabled}
                      onClick={() => choose(option.value)}
                    >
                      <span className="menu-item-copy">
                        <span className="menu-item-label">
                          {option.leadingIcon}
                          {option.ai && <Zap size={12} className="panel-select-ai" aria-label="AI column" />}
                          {option.label}
                        </span>
                        {option.description && (
                          <span className="menu-item-description">{option.description}</span>
                        )}
                      </span>
                      {option.value === selectedValue && <Check size={13} className="panel-select-check" aria-hidden />}
                    </button>
                  </div>
                );
              })
            )}
          </AnchoredMenu>}
    </>
  );
});
