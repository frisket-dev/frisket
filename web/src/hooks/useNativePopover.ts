import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';

export interface NativePopoverOptions {
  /** Attach listeners / show only while true (default true). Gate on open state. */
  enabled?: boolean;
  /**
   * `manual` (default) grants no UA light-dismiss — the site drives its own
   * Escape/outside handling below (the exact `escape: false` sites need this so
   * the UA never adds an Escape-dismiss that was deliberately absent). `auto`
   * hands Escape + outside-pointerdown to the UA; the site passes no explicit
   * handlers and relies on the `toggle` event to re-sync React state.
   */
  mode?: 'auto' | 'manual';
  /** manual mode: Escape dismisses (default true). */
  escape?: boolean;
  /** manual mode: outside-pointerdown dismisses (default true). */
  outside?: boolean;
  /** manual mode: outside targets matching this selector do NOT dismiss (the trigger). */
  ignoreSelector?: string;
  /** manual mode: extra containers counted as "inside" — for portaled menus. */
  extraRefs?: ReadonlyArray<RefObject<HTMLElement | null>>;
  /** Restore focus to the element focused when this opened (default false). */
  focusRestore?: boolean;
}

/**
 * Drives a React-owned, conditionally-rendered element as a native **Popover
 * API** element in the browser TOP LAYER. This is the shared successor behind
 * the anchored-surface migration that retired `useDismissable`'s pointerdown
 * machinery AND its `OVERLAY_OPEN_SELECTORS` registry.
 *
 * What top-layer membership buys us: the element paints above any later overlay
 * with **no z-index management**, and it participates in the native Escape
 * nesting order — which is why resident panels can now defer to `:popover-open`
 * structurally (`useEscapeDismiss`) instead of consulting a hand-maintained
 * selector list that drifted. `overlayAware` / `OVERLAY_OPEN_SELECTORS` retire
 * with this hook; the typing-guard moves to `useEscapeDismiss`; a popover's own
 * escape/outside/ignoreSelector/focusRestore dismissal survives here.
 *
 * The element must be conditionally rendered (mounted only while open), so this
 * effect's mount == "open". The `popover` attribute is set imperatively because
 * stable @types/react (18.3) still lacks the `popover` prop (canary only).
 *
 * Positioning stays with JS (`useAnchoredPosition` / inline `left`/`top`): the
 * caller resets the UA popover centering (`inset: auto; margin: 0`) on the
 * element so its own placement applies. Popover-for-behavior, JS-for-placement.
 */
export function useNativePopover(
  ref: RefObject<HTMLElement | null>,
  onClose: () => void,
  opts: NativePopoverOptions = {},
): void {
  const { enabled = true, mode = 'manual' } = opts;

  // Latest onClose + opts without re-subscribing every render, so callers may
  // pass inline callbacks / refs freely. Synced in an effect (post-commit).
  const latest = useRef({ onClose, opts });
  useEffect(() => {
    latest.current = { onClose, opts };
  });

  // Top-layer lifecycle: set the popover attribute and show it while mounted.
  useLayoutEffect(() => {
    if (!enabled) return undefined;
    const el = ref.current;
    if (!el) return undefined;
    const supportsPopover = typeof el.showPopover === 'function' && typeof el.hidePopover === 'function';
    if (supportsPopover && el.getAttribute('popover') !== mode) el.setAttribute('popover', mode);
    // In auto mode a UA `toggle` with newState 'closed' is the light-dismiss
    // (Escape / outside-pointerdown) — forward it so React state re-syncs.
    const onToggle = (event: Event) => {
      if (mode === 'auto' && (event as ToggleEvent).newState === 'closed') {
        latest.current.onClose();
      }
    };
    el.addEventListener('toggle', onToggle);
    if (supportsPopover && el.isConnected && !el.matches(':popover-open')) el.showPopover();
    return () => {
      el.removeEventListener('toggle', onToggle);
      if (supportsPopover && el.isConnected && el.matches(':popover-open')) el.hidePopover();
    };
  }, [ref, enabled, mode]);

  // Manual-mode explicit dismissal (ported per-site from the old useDismissable
  // call) — capture-phase outside-pointerdown + Escape, minus the retired
  // overlay registry. Auto mode skips this entirely (the UA owns it).
  useEffect(() => {
    if (!enabled || mode === 'auto') return undefined;
    const { escape = true, outside = true } = latest.current.opts;

    const restoreTarget = latest.current.opts.focusRestore
      ? (document.activeElement as HTMLElement | null)
      : null;
    const dismiss = () => {
      latest.current.onClose();
      if (restoreTarget) requestAnimationFrame(() => restoreTarget.focus());
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      const target = event.target;
      const { opts: o } = latest.current;
      // A nested top-layer surface owns its first Escape. The parent already
      // names those surfaces in extraRefs for outside-pointer handling; honor
      // the same boundary here instead of dismissing both levels at once.
      if (target instanceof Node && o.extraRefs?.some((r) => r.current?.contains(target))) return;
      dismiss();
    };
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      const { opts: o } = latest.current;
      if (ref.current?.contains(target)) return;
      if (o.extraRefs?.some((r) => r.current?.contains(target))) return;
      if (o.ignoreSelector && target instanceof Element && target.closest(o.ignoreSelector)) return;
      dismiss();
    };

    if (escape) document.addEventListener('keydown', onKeyDown, true);
    if (outside) document.addEventListener('pointerdown', onPointerDown, true);
    return () => {
      if (escape) document.removeEventListener('keydown', onKeyDown, true);
      if (outside) document.removeEventListener('pointerdown', onPointerDown, true);
    };
    // `enabled`/`mode` decide whether these listeners exist; the escape/outside
    // flags are read live from `latest` at attach time. Re-run only on those.
  }, [ref, enabled, mode]);
}
