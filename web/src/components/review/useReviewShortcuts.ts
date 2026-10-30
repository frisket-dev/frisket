import { useEffect } from 'react';

/** Shared Review keyboard vocabulary; each controller owns the actions. */
export function useReviewShortcuts({ disabled, onMove, onAccept, onReject, onEdit, onAcceptRemaining }: {
  disabled: boolean; onMove(delta: -1 | 1): void; onAccept(): void; onReject(): void;
  onEdit?(): void; onAcceptRemaining?(): void;
}) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.isComposing || event.ctrlKey || event.metaKey || event.altKey || disabled) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target?.closest('input, textarea, select, [contenteditable="true"], [role="menu"], [role="listbox"]')) return;
      if (document.querySelector(':popover-open')) return;
      const modals = document.querySelectorAll(':modal');
      const top = modals[modals.length - 1];
      if (top && (!top.classList.contains('review-overlay') || (target && !top.contains(target)))) return;
      switch (event.key.toLowerCase()) {
        case 'arrowdown': case 's': case 'j': onMove(1); break;
        case 'arrowup': case 'w': case 'k': onMove(-1); break;
        case 'arrowleft': onAccept(); break;
        case 'a': if (event.shiftKey && onAcceptRemaining) onAcceptRemaining(); else onAccept(); break;
        case 'arrowright': case 'd': case 'r': onReject(); break;
        case 'e': if (onEdit) onEdit(); else return; break;
        default: return;
      }
      event.preventDefault();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [disabled, onMove, onAccept, onReject, onEdit, onAcceptRemaining]);
}
