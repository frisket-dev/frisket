import { FileText } from 'lucide-react';
import { openEvidenceViewerViaGlobalEvent } from './openEvidenceViewerViaGlobalEvent';

// The reusable chip behind RowDrawer's per-cell evidence links, so a user
// never sees a raw `evidence_link:*` id -- only a clickable chip.
//
// onOpen(linkId) is a REQUIRED-shaped prop with a default implementation
// (dispatch the existing global `frisket:open-evidence` window event) so a
// caller can override it to drive a LOCAL docked pane instead of the global
// event -- a hardcoded global dispatch would make this component
// non-reusable there.

export interface CitationChipProps {
  /** The evidence_link stable_id this chip opens the viewer at. */
  linkId: string;
  /** Leading label text, e.g. "Evidence", "Stale evidence", "Citation c1". */
  label: string;
  /** Optional quote/snippet shown under the label, small + muted. */
  snippet?: string | null;
  /** Defaults to the global frisket:open-evidence event (see above) --
   *  callers that dock evidence locally (e.g. a grounded-answers pane)
   *  override this instead of relying on the global event. */
  onOpen?: (linkId: string) => void;
  /** Extra class(es) layered onto the base `cell-evidence-link` look
   *  (e.g. 'cell-evidence-link-stale' for the stale-evidence variant). */
  className?: string;
  testId?: string;
  disabled?: boolean;
}

export function CitationChip({
  linkId,
  label,
  snippet,
  onOpen = openEvidenceViewerViaGlobalEvent,
  className,
  testId,
  disabled = false,
}: CitationChipProps) {
  return (
    <button
      type="button"
      className={`cell-evidence-link${className ? ` ${className}` : ''}`}
      data-testid={testId}
      disabled={disabled}
      onClick={() => onOpen(linkId)}
    >
      <FileText size={13} />
      <span>
        {label}
        {snippet && <small>{snippet}</small>}
      </span>
    </button>
  );
}
