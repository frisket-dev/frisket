import '../FormControls.css';
// The sticky "N selected · [actions] · ✕" bar that appears while a ValueList
// selection is non-empty. Renders nothing at count 0 — visibility is purely a
// function of the controlled count. Action buttons (＋ New bucket, Add to ▾, …)
// are the caller's children so each resolve action brings its own verbs.
import type { ReactNode } from 'react';
import { X } from 'lucide-react';
import { IconButton } from '../IconButton';
import './resolve.css';

export function SelectionBar({
  count,
  onClear,
  children,
  testId = 'resolve-selection-bar',
}: {
  /** Number of selected values; the bar renders only when > 0. */
  count: number;
  /** Dismiss ✕ — clears the selection (parent owns the Set). */
  onClear(): void;
  /** Action buttons shown beside the count. */
  children?: ReactNode;
  testId?: string;
}) {
  if (count <= 0) return null;
  return (
    <div
      className="control-row resolve-selection-bar"
      data-testid={testId}
      role="toolbar"
      aria-label="Selection actions"
    >
      <span className="resolve-selection-count" data-testid="resolve-selection-count">
        {count.toLocaleString()} selected
      </span>
      <div className="control-row resolve-selection-actions">{children}</div>
      <IconButton
        icon={X}
        data-testid="resolve-selection-clear"
        label="Clear selection"
        onClick={onClear}
      />
    </div>
  );
}
