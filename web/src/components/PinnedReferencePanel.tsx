import './PinnedReferencePanel.css';
import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { MenuPop } from './MenuPop';
import { WindowCloseButton } from './WindowCloseButton';
import { useNativePopover } from '../hooks/useNativePopover';

/** A reference that stays open while its neighboring control is used. When
 * there is room, it sits to the control's left; otherwise it remains in flow. */
export function PinnedReferencePanel({ id, title, anchorRef, sourceRef, onClose, children }: {
  id: string;
  title: string;
  anchorRef: RefObject<HTMLElement | null>;
  sourceRef?: RefObject<HTMLElement | null>;
  onClose(): void;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const [preferredWidth, setPreferredWidth] = useState(550);
  const [portalRoot, setPortalRoot] = useState<Element | null>(null);
  const [position, setPosition] = useState<CSSProperties | null>(null);
  useLayoutEffect(() => {
    const anchor = anchorRef.current;
    if (!anchor) return;
    setPortalRoot(anchor.closest('dialog') ?? document.body);
    const measure = () => {
      const rect = anchor.getBoundingClientRect();
      const gap = 12;
      const margin = 8;
      const availableWidth = rect.left - gap - margin;
      if (availableWidth < 320 || window.innerWidth < 760) {
        setPosition(null);
        return;
      }
      const top = Math.max(margin, Math.min(rect.top, window.innerHeight - 200));
      setPosition({ position: 'fixed', inset: 'auto', margin: 0, top,
        right: window.innerWidth - rect.left + gap, width: Math.min(preferredWidth, availableWidth),
        maxWidth: availableWidth, maxHeight: window.innerHeight - top - margin });
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(anchor);
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [anchorRef, preferredWidth]);
  const beside = position !== null;
  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (!beside || !panel || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => setPreferredWidth(Math.round(panel.getBoundingClientRect().width)));
    observer.observe(panel);
    return () => observer.disconnect();
  }, [beside]);
  useNativePopover(panelRef, onClose, {
    enabled: position !== null && portalRoot !== null, outside: false, escape: false, sourceRef,
  });
  const panel = <MenuPop ref={panelRef} id={id} role="region" aria-label={title}
    className="pinned-reference-panel" data-layout={position ? 'beside' : 'stacked'}
    style={position ?? undefined}>
    <header className="pinned-reference-heading">
      <h3>{title}</h3>
      <WindowCloseButton label={`Close ${title.toLowerCase()}`} onClick={onClose} />
    </header>
    <div className="pinned-reference-content">{children}</div>
  </MenuPop>;
  return position && portalRoot ? createPortal(panel, portalRoot) : panel;
}
