// Chip/token multi-select for column lists — replaces the raw multi-select
// as the input step shared by Semantic join, Reduce, and Agent.
// Selected columns show as removable chips (AI columns get the ⚡ purple
// treatment); a portaled dropdown lists all columns with a filter and a
// checkbox per row. The real state is a string[] of column names.

import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Check, Zap } from 'lucide-react';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import { useNativePopover } from '../hooks/useNativePopover';

export interface MultiColumnOption {
  name: string;
  type: string;
  ai?: boolean;
}

export function MultiColumnPicker({
  value,
  options,
  onValueChange,
  testId,
  ariaLabel,
  disabled = false,
}: {
  value: string[];
  options: MultiColumnOption[];
  onValueChange(value: string[]): void;
  testId?: string;
  ariaLabel?: string;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const boxRef = useRef<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const menuPos = useAnchoredPosition(boxRef, {
    enabled: open,
    width: (rect) => rect.width,
    minHeight: 180,
  });

  const optionByName = useMemo(() => {
    const map = new Map<string, MultiColumnOption>();
    for (const option of options) map.set(option.name, option);
    return map;
  }, [options]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // Native top-layer popover: a STRUCTURAL `:popover-open, :modal` check in
  // useEscapeDismiss replaced OVERLAY_OPEN_SELECTORS — but this menu stayed a
  // plain portaled div, so it was invisible to that check. A resident panel
  // hosting this picker (e.g. ActionDrawer) would
  // close on Escape even with the picker's dropdown open and focus off its
  // filter input (the typing guard's blind spot), because listener order
  // can't save it: the resident's document-capture Escape handler is
  // registered at the resident's OWN mount, always before this menu opens.
  // Only a real top-layer element (`:popover-open` true) is deferred to
  // regardless of registration order. `extraRefs: [boxRef]` keeps the
  // trigger box "inside" so its own opening click never double-fires close.
  // `escape: false`: Escape dismissal stays owned by the filter input's own
  // onKeyDown below (only fires when the input has focus). A blurred Escape
  // does nothing to the picker itself; the
  // `:popover-open` membership above is what correctly keeps a resident
  // host (e.g. ActionDrawer) from closing on that same blurred Escape.
  useNativePopover(menuRef, () => {
    setOpen(false);
    setFilter('');
  }, {
    enabled: open,
    extraRefs: [boxRef],
    escape: false,
  });

  const toggle = (name: string) => {
    onValueChange(value.includes(name) ? value.filter((n) => n !== name) : [...value, name]);
  };
  const remove = (name: string) => onValueChange(value.filter((n) => n !== name));

  const filtered = options.filter((o) => o.name.toLowerCase().includes(filter.trim().toLowerCase()));

  return (
    <>
      <div
        ref={boxRef}
        className={`multi-col-picker${open ? ' is-open' : ''}`}
        data-testid={testId}
        role="group"
        aria-label={ariaLabel}
        aria-disabled={disabled}
        onMouseDown={(event) => {
          if (disabled) return;
          // Clicks on the box (not a chip's ×) open the dropdown.
          if ((event.target as HTMLElement).closest('.multi-col-chip-remove')) return;
          if (!open) {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        {value.map((name) => {
          const option = optionByName.get(name);
          return (
            <span key={name} className={`multi-col-chip${option?.ai ? ' is-ai' : ''}`}>
              {option?.ai && <Zap size={11} aria-hidden />}
              {name}
              <button
                type="button"
                className="multi-col-chip-remove"
                aria-label={`Remove ${name}`}
                disabled={disabled}
                onClick={() => remove(name)}
              >
                ×
              </button>
            </span>
          );
        })}
        {open ? (
          <input
            ref={inputRef}
            className="multi-col-filter"
            placeholder="Filter columns…"
            value={filter}
            aria-label="Filter columns"
            onChange={(e) => setFilter(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                setOpen(false);
                setFilter('');
              } else if (e.key === 'Enter') {
                // Never let Enter bubble to the panel's <form> and run the action;
                // instead add the first not-yet-selected match.
                e.preventDefault();
                const next = filtered.find((option) => !value.includes(option.name));
                if (next) {
                  toggle(next.name);
                  setFilter('');
                }
              }
            }}
          />
        ) : (
          <span className="multi-col-add">+ column</span>
        )}
        <span className="multi-col-caret" aria-hidden>▾</span>
      </div>
      {open && !disabled &&
        createPortal(
          <div
            ref={menuRef}
            className="multi-col-menu"
            role="listbox"
            data-testid={testId ? `${testId}-menu` : undefined}
            style={
              menuPos
                ? { top: menuPos.top, bottom: menuPos.bottom, left: menuPos.left, width: menuPos.width, maxHeight: menuPos.maxHeight }
                : { visibility: 'hidden' }
            }
          >
            {filtered.length === 0 && <div className="multi-col-empty">No matching columns</div>}
            {filtered.map((option) => {
              const selected = value.includes(option.name);
              return (
                <button
                  key={option.name}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  className={`multi-col-option${selected ? ' is-selected' : ''}`}
                  onClick={() => toggle(option.name)}
                >
                  <span className={`multi-col-check${selected ? ' is-selected' : ''}`}>
                    {selected && <Check size={11} aria-hidden />}
                  </span>
                  <span className="multi-col-option-name">
                    {option.ai && <Zap size={11} className="multi-col-ai" aria-hidden />}
                    {option.name}
                  </span>
                  <span className="multi-col-option-type">{option.type}</span>
                </button>
              );
            })}
          </div>,
          document.body,
        )}
    </>
  );
}
