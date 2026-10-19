import { useRef, useState } from 'react';
import { CircleHelp } from 'lucide-react';

import type { ActionTemplate } from '../../api/types';
import { AnchoredMenu } from '../MenuPop';
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
  const content = buildActionHelp(action);


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
      {open && (
        <AnchoredMenu
          anchorRef={triggerRef}
          onClose={() => setOpen(false)}
          width={(_rect, viewportWidth) => Math.min(360, viewportWidth - 16)}
          align="right"
          className="action-help-popover"
          data-testid="action-help-popover"
          role="dialog"
          aria-label={`About ${action.name}`}
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
        </AnchoredMenu>
      )}
    </>
  );
}
