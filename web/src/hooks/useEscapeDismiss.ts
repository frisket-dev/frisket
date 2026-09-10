import { useEffect, useRef } from 'react';

/**
 * The surviving Escape-dismiss primitive for RESIDENT panels, after the Popover
 * API / native `<dialog>` migration retired `useDismissable` and its
 * `OVERLAY_OPEN_SELECTORS` registry. A resident panel (ActionDrawer,
 * InspectDetailColumn, Drawer, the ReviewQueue bundle) must close on Escape —
 * but NOT when the Escape belongs to something layered above it. The two guards
 * this hook keeps are:
 *
 * 1. **Top-layer awareness (structural, always on).** Every floating overlay is
 *    now a real top-layer element — a native popover (`:popover-open`) or a
 *    modal `<dialog>` (`:modal`). The browser dismisses the topmost one on
 *    Escape automatically, so a resident must defer whenever ANY top-layer
 *    element is open. `document.querySelector(':popover-open, :modal')` replaces
 *    the hand-maintained `OVERLAY_OPEN_SELECTORS` string that used to simulate
 *    this ordering for non-top-layer divs (and drifted, causing the
 *    double-dismiss bug). This is the whole payoff: the nesting order is now the
 *    browser's, not a selector list's.
 * 2. **Typing guard (opt-in).** Escape inside a real inline editor cancels the
 *    edit, so it must not also tear down the panel hosting it — see
 *    `isPerceivableTypingTarget` below.
 *
 * Capture phase is deliberate: the glide-data-grid canvas swallows Escape when
 * focused, so a bubble-phase listener never sees it.
 */

/**
 * "The Escape belongs to a text-entry surface" selectors. Moved here from the
 * deleted `useDismissable` (its `TYPING_TARGET_SELECTORS`) as this hook is now
 * their only consumer.
 */
const TYPING_TARGET_SELECTORS = [
  'input',
  'textarea',
  'select',
  '[contenteditable="true"]',
  '.gdg-growing-entry',
  '.clip-region',
].join(', ');

/**
 * A typing-target match only counts if it is a REAL, perceivable editing
 * surface. glide-data-grid mounts an always-present proxy `<textarea>` behind
 * its cell overlays (e.g. `.gdg-md-edit-textarea` for markdown cells, styled
 * `width: 0; height: 0; opacity: 0`) purely to keep the canvas's own
 * keyboard/focus plumbing working — it autofocuses even when no visible editor
 * is showing. A zero-size or invisible match is that kind of proxy, not
 * something the user is actually typing into, so Escape must fall through to
 * dismissal instead of being swallowed. Checked by geometry/style rather than
 * by name so any future invisible proxy stays covered without another exclusion
 * list.
 */
function isPerceivableTypingTarget(el: Element): boolean {
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return false;
  const style = window.getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none') return false;
  if (Number(style.opacity) === 0) return false;
  return true;
}

export interface EscapeDismissOptions {
  /** Attach the listener only while true (default true). Gate on the open state. */
  enabled?: boolean;
  /** Ignore Escape whose target is a real text-entry surface (default false). */
  typingGuard?: boolean;
}

/**
 * Close `onClose` on Escape, deferring to any open top-layer overlay and
 * (optionally) to an in-progress inline edit.
 */
export function useEscapeDismiss(
  onClose: () => void,
  opts: EscapeDismissOptions = {},
): void {
  const { enabled = true, typingGuard = false } = opts;

  // Latest onClose without re-subscribing every render, so callers may pass
  // inline callbacks freely. Synced in an effect (not during render) — the
  // handler only reads it post-commit.
  const latest = useRef(onClose);
  useEffect(() => {
    latest.current = onClose;
  });

  useEffect(() => {
    if (!enabled) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      const target = event.target as HTMLElement | null;
      if (typingGuard) {
        const typingTarget = target?.closest(TYPING_TARGET_SELECTORS) ?? null;
        if (typingTarget && isPerceivableTypingTarget(typingTarget)) return;
      }
      // A native popover or modal <dialog> is open above us — the browser will
      // dismiss it first; this Escape is not ours.
      if (document.querySelector(':popover-open, :modal')) return;
      latest.current();
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [enabled, typingGuard]);
}
