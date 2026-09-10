import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { CircleHelp } from 'lucide-react';

import type { ActionTemplate } from '../../api/types';
import { useAnchoredPosition } from '../../hooks/useAnchoredPosition';
import { buildActionHelp } from './actionHelpModel';

export function ActionHelpPopover({
  action,
  className = '',
}: {
  action: ActionTemplate;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLElement | null>(null);
  const position = useAnchoredPosition(triggerRef, {
    enabled: open,
    width: (_rect, viewportWidth) => Math.min(360, viewportWidth - 16),
    align: 'right',
    gap: 6,
    minHeight: 220,
  });
  const content = buildActionHelp(action);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (triggerRef.current?.contains(target) || popoverRef.current?.contains(target)) return;
      setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', closeOnEscape, true);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', closeOnEscape, true);
    };
  }, [open]);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={`icon-btn action-help-trigger ${className}`.trim()}
        data-testid="action-help-button"
        aria-label={`About ${action.name}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        title={`About ${action.name}`}
        onClick={() => setOpen((current) => !current)}
      >
        <CircleHelp size={16} aria-hidden />
      </button>
      {open && createPortal(
        <section
          ref={popoverRef}
          className="action-help-popover"
          data-testid="action-help-popover"
          role="dialog"
          aria-label={`About ${action.name}`}
          style={position ? {
            top: position.top,
            bottom: position.bottom,
            left: position.left,
            width: position.width,
            maxHeight: position.maxHeight,
          } : { visibility: 'hidden' }}
        >
          <header>
            <span className="action-help-kicker">ABOUT THIS ACTION</span>
            <h3>{action.name}</h3>
            <code>{action.actionKind ?? action.kind}</code>
          </header>
          <p>{content.summary}</p>
          <div className="action-help-section">
            <h4>How it works</h4>
            <p>{content.how}</p>
          </div>
          <div className="action-help-section">
            <h4>What it needs</h4>
            <ul>
              {content.inputs.map((item, index) => (
                <li key={`${item.label}-${index}`}>
                  <strong>{item.label}</strong>
                  {item.detail ? ` — ${item.detail}` : ''}
                </li>
              ))}
            </ul>
          </div>
          <div className="action-help-section">
            <h4>What it produces</h4>
            <p>{content.output}</p>
          </div>
          <div className="action-help-section">
            <h4>Cost and requirements</h4>
            <p>{content.cost}</p>
          </div>
          <div className="action-help-section">
            <h4>Good for</h4>
            <p>{content.goodFor}</p>
          </div>
        </section>,
        document.body,
      )}
    </>
  );
}
