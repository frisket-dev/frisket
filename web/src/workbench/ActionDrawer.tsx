// Action drawer: a fixed-400px right-docked pane that re-hosts the ActionPanel
// form. While open, the shell reserves the same width in its work regions so
// the grid, ribbon, tabs, and Monitor end at the drawer's left edge.
//
// FULL-HEIGHT: the vertical band runs from the top chrome's
// (project bar) BOTTOM edge down to the shell's bottom edge — the drawer now
// aligns beside the Monitor dock as well as the ribbon + sheet-tab strip. The
// project bar remains full width above it.
//
// Geometry is MEASURED from the live layout (not magic numbers): the drawer is
// an absolutely-positioned sibling inside `.workbench-shell` whose top tracks
// the chrome bar (bottom is simply the shell's own bottom edge, `bottom: 0`).
// A ResizeObserver keeps it in sync as the chrome bar changes height.

import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { useEscapeDismiss } from '../hooks/useEscapeDismiss';

const DRAWER_WIDTH = 400;

interface Band {
  top: number;
}

function measureBand(el: HTMLElement): Band | null {
  const shell = el.closest('[data-testid="workbench-shell"]') as HTMLElement | null;
  if (!shell) return null;
  const shellRect = shell.getBoundingClientRect();
  const chromeBar = shell.querySelector<HTMLElement>('[data-testid="chrome-bar"]');
  // Top of the band = the top chrome's (project bar) BOTTOM edge, so the open
  // drawer covers the ribbon + sheet-tab strip beneath it while the project
  // bar itself stays visible. Falls back to the Navigate (sheet-tab) row's
  // top — the pre-full-height band — if the chrome bar is momentarily absent.
  const top = chromeBar
    ? chromeBar.getBoundingClientRect().bottom - shellRect.top
    : (
        shell.querySelector<HTMLElement>('[data-testid="workbench-region-navigate"]') ??
        shell.querySelector<HTMLElement>('.workbench-main-row') ??
        shell
      ).getBoundingClientRect().top - shellRect.top;
  return { top: Math.max(0, top) };
}

export function ActionDrawer({
  onClose,
  children,
}: {
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [band, setBand] = useState<Band | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => setBand(measureBand(el));
    update();
    const shell = el.closest('[data-testid="workbench-shell"]') as HTMLElement | null;
    shell?.classList.add('action-drawer-open');
    shell?.style.setProperty('--action-drawer-width', `${DRAWER_WIDTH}px`);
    if (!shell || typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', update);
      return () => {
        shell?.classList.remove('action-drawer-open');
        shell?.style.removeProperty('--action-drawer-width');
        window.removeEventListener('resize', update);
      };
    }
    const observer = new ResizeObserver(update);
    observer.observe(shell);
    // Only the element the band is actually measured from needs observing —
    // the chrome bar (top anchor). The bottom edge is `bottom: 0` (the shell's
    // own bottom, covering the Monitor dock), so the dock no longer needs
    // observing; the Navigate/Act regions never bound the band either now
    // that the drawer covers them while open.
    const chromeBar = shell.querySelector('[data-testid="chrome-bar"]');
    if (chromeBar) observer.observe(chromeBar);
    window.addEventListener('resize', update);
    return () => {
      shell.classList.remove('action-drawer-open');
      shell.style.removeProperty('--action-drawer-width');
      observer.disconnect();
      window.removeEventListener('resize', update);
    };
  }, []);

  // Esc closes the drawer (companion to the × affordance). Same top-layer-aware
  // pattern as InspectDetailColumn: an Escape that belongs to something else
  // must not also dismiss the drawer — a text input/editor (typing guard), or a
  // floating menu / modal / cost gate / palette layered above (now a real
  // top-layer element, so `useEscapeDismiss` defers to `:popover-open, :modal`
  // structurally instead of matching a registry selector string).
  useEscapeDismiss(onClose, { typingGuard: true });

  const style: React.CSSProperties = band
    ? { top: band.top, bottom: 0, width: DRAWER_WIDTH }
    : { top: 0, bottom: 0, width: DRAWER_WIDTH, visibility: 'hidden' };

  return (
    <section
      ref={ref}
      className="action-drawer"
      data-testid="action-drawer"
      aria-label="Action drawer"
      style={style}
    >
      {children}
    </section>
  );
}
