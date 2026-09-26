// The add-column popover uses the native top layer and owns dismissal.
import { useCallback, useEffect, useRef, useState } from 'react';
import { Plus } from 'lucide-react';
import { useNativePopover } from '../hooks/useNativePopover';
import { clampLeft } from '../hooks/useAnchoredPosition';
import { PanelSelect } from '../components/PanelSelect';

const ADD_COLUMN_TYPES = [
  'text', 'number', 'integer', 'boolean', 'category', 'json', 'date',
] as const;

// The add-column popover: an inline name + type prompt (menu-pop idiom) anchored
// at its trigger (the header "+" or the caret Insert-left/right item). Submitting
// creates the column via the v1 column.add action.
export function AddColumnPopover({
  anchor,
  existingNames,
  onSubmit,
  onClose,
}: {
  anchor: { x: number; y: number };
  existingNames: string[];
  onSubmit(name: string, type: string): void | Promise<void>;
  onClose(): void;
}) {
  const [name, setName] = useState('');
  const [type, setType] = useState<string>('text');
  const inputRef = useRef<HTMLInputElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const triggerCaptured = useRef(false);
  const dismiss = useCallback(() => {
    onClose();
    // Hand focus back to the "+" trigger the popover was summoned from, instead
    // of stranding it on <body>. Deferred a frame so React unmounts the popover
    // (and its focused field) first.
    const trigger = triggerRef.current;
    if (trigger && typeof trigger.focus === 'function') {
      requestAnimationFrame(() => trigger.focus());
    }
  }, [onClose]);
  // A native top-layer popover: resident panels defer to it structurally
  // (`:popover-open`) — no manual overlay-registry check needed. It owns its
  // own Escape + outside-pointerdown dismissal (the "+" trigger is ignored so
  // re-clicks re-anchor via the trigger's own handler).
  useNativePopover(popoverRef, dismiss, { ignoreSelector: '.grid-add-column-button' });
  useEffect(() => {
    // Capture the element focused when the popover opened (the "+" trigger) ONCE,
    // before we move focus into the name field — the one-shot guard keeps
    // StrictMode's remount from re-capturing the now-focused field.
    if (!triggerCaptured.current) {
      triggerCaptured.current = true;
      triggerRef.current = document.activeElement as HTMLElement | null;
    }
    inputRef.current?.focus();
  }, []);
  const trimmed = name.trim();
  const duplicate = existingNames.some((existing) => existing === trimmed);
  const canSubmit = trimmed.length > 0 && !duplicate;
  // 240px popover + a 12px right gutter reserved (252 = 240 + 12).
  const left = clampLeft(anchor.x, 252, typeof window !== 'undefined' ? window.innerWidth : 1200);
  const top = Math.max(8, anchor.y + 4);
  return (
    <div
      ref={popoverRef}
      className="grid-header-menu grid-add-column-popover"
      data-testid="grid-add-column-popover"
      aria-label="Add column"
      // Reset the UA popover centering (inset:0; margin:auto) so the JS-anchored
      // left/top still place it in the top layer.
      style={{ left, top, width: 240, right: 'auto', bottom: 'auto', margin: 0 }}
    >
      <div className="grid-header-menu-title">
        <span>Add column</span>
      </div>
      <label className="grid-add-column-field">
        <span>Name</span>
        <input
          ref={inputRef}
          type="text"
          className="form-input"
          data-testid="grid-add-column-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && canSubmit) {
              event.preventDefault();
              void onSubmit(trimmed, type);
            }
          }}
        />
      </label>
      <label className="grid-add-column-field">
        <span>Type</span>
        <PanelSelect
          className="row-height-select"
          data-testid="grid-add-column-type"
          value={type}
          onChange={(event) => setType(event.target.value)}
        >
          {ADD_COLUMN_TYPES.map((option) => (
            <option key={option} value={option}>{option}</option>
          ))}
        </PanelSelect>
      </label>
      {duplicate && (
        <div className="grid-add-column-error" data-testid="grid-add-column-error">
          A column named “{trimmed}” already exists.
        </div>
      )}
      <div className="grid-add-column-actions">
        <button
          type="button"
          className="grid-header-menu-item"
          data-testid="grid-add-column-cancel"
          onClick={onClose}
        >
          Cancel
        </button>
        <button
          type="button"
          className="grid-header-menu-item grid-add-column-submit"
          data-testid="grid-add-column-submit"
          disabled={!canSubmit}
          onClick={() => void onSubmit(trimmed, type)}
        >
          <Plus size={14} aria-hidden />
          Add column
        </button>
      </div>
    </div>
  );
}
