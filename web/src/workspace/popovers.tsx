// Overlay popovers: the copilot dialog popover (chrome ✧ toggle) and the
// add-column popover. Both are native TOP-LAYER popovers (useNativePopover) that
// own document-level dismissal.
import { useCallback, useEffect, useRef, useState } from 'react';
import { Plus } from 'lucide-react';
import { useNativePopover } from '../hooks/useNativePopover';
import { clampLeft } from '../hooks/useAnchoredPosition';
import { CopilotWorkbenchPanel } from '../workbench/contributions';
import { PanelSelect } from '../components/PanelSelect';

// The copilot popover: a floating overlay summoned
// by the chrome ✧ toggle. It is a native top-layer popover, so resident panels
// defer to it structurally (`:popover-open`) — no manual overlay-registry
// check needed. It still owns its own Escape + outside-pointerdown dismissal
// (the ✧ trigger is ignored so its own toggle handles re-clicks).
export function CopilotDialogPopover({
  onClose,
  ...panelProps
}: Parameters<typeof CopilotWorkbenchPanel>[0]) {
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const triggerCaptured = useRef(false);
  // The header chevron (and a proposal's Inspect button)
  // collapse the popover to a header-only strip. Per-mount state — never
  // persisted; closing and reopening the popover starts expanded. Owned here
  // (not in CopilotPanel) because the collapsed strip must also survive
  // outside-pointerdown — see the pointerdown effect below.
  const [collapsed, setCollapsed] = useState(false);
  const collapsedRef = useRef(collapsed);
  useEffect(() => {
    // Synced post-commit (react-hooks/refs forbids ref writes during render);
    // the pointerdown handler below reads it, never render output.
    collapsedRef.current = collapsed;
  }, [collapsed]);
  const dismiss = useCallback(() => {
    onClose?.();
    // Hand focus back to the ✧ trigger the popover was summoned from, instead
    // of stranding it on <body>. Deferred a frame so React unmounts the
    // popover first.
    const trigger = triggerRef.current;
    if (trigger && typeof trigger.focus === 'function') {
      requestAnimationFrame(() => trigger.focus());
    }
  }, [onClose]);
  // outside: false — outside-pointerdown dismissal is handled below with a
  // collapsed guard (the hook reads its escape/outside flags once at attach).
  // Escape still closes even while collapsed: the drawer's own useEscapeDismiss
  // defers to `:popover-open`, so a swallowed Escape would make the key inert.
  useNativePopover(popoverRef, dismiss, {
    ignoreSelector: '.chrome-copilot-btn',
    outside: false,
  });
  useEffect(() => {
    // Outside-pointerdown dismissal (the useNativePopover manual-mode behavior,
    // inlined so the COLLAPSED strip is sticky): expanded → an outside click
    // light-dismisses as before; collapsed → the strip stays put so the user
    // can work in the drawer a proposal's Inspect opened and re-expand later.
    // .model-picker-menu is portaled to <body> (top layer), so a pointerdown
    // in the copilot's own model picker must read as "inside" — without this
    // the panel dismissed mid-selection.
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (collapsedRef.current) return;
      if (popoverRef.current?.contains(target)) return;
      if (
        target instanceof Element &&
        target.closest('.chrome-copilot-btn, .model-picker-menu')
      ) {
        return;
      }
      dismiss();
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [dismiss]);
  useEffect(() => {
    // Capture the trigger once so StrictMode remounts do not recapture the focused popover.
    if (!triggerCaptured.current) {
      triggerCaptured.current = true;
      triggerRef.current = document.activeElement as HTMLElement | null;
    }
  }, []);
  return (
    <div
      ref={popoverRef}
      className="copilot-popover"
      data-testid="copilot-popover"
      aria-label="Copilot"
      // Reset the UA popover centering (inset:0; margin:auto) so the .copilot-
      // popover CSS anchoring (right/bottom) still applies in the top layer.
      style={{ top: 'auto', left: 'auto', margin: 0 }}
    >
      <CopilotWorkbenchPanel
        {...panelProps}
        onClose={dismiss}
        collapsed={collapsed}
        onCollapsedChange={setCollapsed}
      />
    </div>
  );
}

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
