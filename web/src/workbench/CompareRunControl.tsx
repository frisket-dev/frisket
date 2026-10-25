import type { ReactNode } from 'react';
import { Loader2, Play, Square } from 'lucide-react';

import { Button } from '../components/Button';

export interface CompareRunControlProps {
  testIdPrefix: string;
  /** Presentation only: the caller retains ownership of its job lifecycle. */
  busy: boolean;
  /** Gates Run; cancellation stays available whenever cancellable work is busy. */
  disabled?: boolean;
  onRun(): void;
  /** Omit only for work whose underlying transport cannot cancel. */
  onCancel?(): void;
  workingLabel?: ReactNode;
  /** Optional adjacent pending/running detail, such as "3 pending" or "2 / 5". */
  progress?: ReactNode;
  /** Concise native tooltip explaining the current state or disabled guard. */
  statusDetail?: string;
}

/** Shared run/cancel presentation. Job ownership, consent, and state projection
 * remain in each comparison tab. */
export function CompareRunControl({
  testIdPrefix,
  busy,
  disabled = false,
  onRun,
  onCancel,
  workingLabel = 'Running…',
  progress,
  statusDetail,
}: CompareRunControlProps) {
  return <>
    <Button type="button" className="ocr-compare-run btn-primary"
      data-testid={`${testIdPrefix}-run`} disabled={busy || disabled}
      aria-busy={busy || undefined} title={statusDetail} onClick={onRun}>
      {busy
        ? <Loader2 size={13} className="ocr-compare-spin" aria-hidden />
        : <Play size={13} aria-hidden />}
      <span className="ocr-compare-run-label">{busy ? workingLabel : 'Run comparison'}</span>
    </Button>
    {busy && onCancel && <Button type="button" data-testid={`${testIdPrefix}-cancel`} onClick={onCancel}>
      <Square size={13} aria-hidden /> Cancel
    </Button>}
    {progress !== undefined && <span className="ocr-compare-run-count mono" role="status"
      data-testid={`${testIdPrefix}-run-progress`} title={statusDetail}>{progress}</span>}
  </>;
}
