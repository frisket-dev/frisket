// Replay preserve+surface cell popover: a minimal top-layer popover — NOT the
// deferred fast-scan compare, just two
// labeled values (your edit vs the newer generated value) and two buttons
// (Accept newer / Keep edit). Summoned by the column chip's Review control at
// the first pending cell; the host auto-advances it to the next pending cell
// after either action. A native Popover-API element (useNativePopover) like the
// sibling overlays in popovers.tsx; positioning stays in JS.
import { useRef } from 'react';

import { useNativePopover } from '../hooks/useNativePopover';

function formatReplayValue(value: unknown): string {
  if (value === null || value === undefined) return '(empty)';
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

export function ReplayAcceptPopover({
  columnName,
  yourEdit,
  newerGenerated,
  busy,
  onAccept,
  onKeep,
  onClose,
}: {
  columnName: string;
  yourEdit: unknown;
  newerGenerated: unknown;
  busy: boolean;
  onAccept: () => void;
  onKeep: () => void;
  onClose: () => void;
}) {
  const popoverRef = useRef<HTMLDivElement | null>(null);
  useNativePopover(popoverRef, onClose, {
    ignoreSelector: '.grid-column-annotation',
  });
  return (
    <div
      ref={popoverRef}
      className="replay-accept-popover"
      data-testid="replay-accept-popover"
      aria-label={`Review updated value for ${columnName}`}
      // Popover-for-behavior, JS-for-placement: reset the UA popover centering
      // and anchor a fixed panel just below the header band.
      style={{
        inset: 'auto',
        top: 96,
        left: '50%',
        transform: 'translateX(-50%)',
        margin: 0,
      }}
    >
      <div className="replay-accept-popover-body">
        <label className="replay-accept-field">
          <span className="replay-accept-field-label">Your edit</span>
          <span className="replay-accept-field-value" data-testid="replay-your-edit">
            {formatReplayValue(yourEdit)}
          </span>
        </label>
        <label className="replay-accept-field">
          <span className="replay-accept-field-label">Newer generated</span>
          <span
            className="replay-accept-field-value"
            data-testid="replay-newer-generated"
          >
            {formatReplayValue(newerGenerated)}
          </span>
        </label>
      </div>
      <div className="replay-accept-popover-actions">
        <button
          type="button"
          className="btn btn-primary"
          data-testid="replay-accept-button"
          disabled={busy}
          onClick={onAccept}
        >
          Accept newer
        </button>
        <button
          type="button"
          className="btn"
          data-testid="replay-keep-button"
          disabled={busy}
          onClick={onKeep}
        >
          Keep edit
        </button>
      </div>
    </div>
  );
}
