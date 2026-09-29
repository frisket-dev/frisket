import { useEffect, useState } from 'react';
import { type CellEvidencePayload, type ReviewBundleField } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { type ResolvedMediaValue } from '../../media/resolveMediaValue';
import { DocumentReader } from '../../workbench/DocumentReader';
import { EvidenceViewer } from '../EvidenceViewer';

type ReviewEvidencePreviewState =
  | { phase: 'loading' }
  | { phase: 'ready'; linkId: string }
  | { phase: 'empty' }
  | { phase: 'error' };

function currentEvidenceMatchesField(payload: CellEvidencePayload, field: ReviewBundleField): boolean {
  const ref = payload.current_value_ref;
  if (!ref || typeof ref !== 'object' || Array.isArray(ref)) return false;
  const runId = (ref as Record<string, unknown>).run_id;
  return (ref as Record<string, unknown>).kind === 'run_result' && String(runId) === field.runId;
}

/** Shows current-run citations for the selected result, then the same-row PDF
 * only when no qualifying citation exists. */
export function ReviewSourcePreview({
  activeField,
  sourcePdf,
  onVisibilityChange,
}: {
  activeField: ReviewBundleField | undefined;
  sourcePdf: ResolvedMediaValue | null;
  onVisibilityChange?(visible: boolean): void;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const [state, setState] = useState<ReviewEvidencePreviewState>(() => ({ phase: activeField ? 'loading' : 'empty' }));

  useEffect(() => {
    const field = activeField;
    if (!field) return;
    let active = true;
    api.getCellEvidence(field.rowId, field.columnId).then(
      (payload) => {
        if (!active) return;
        const activeLinks = currentEvidenceMatchesField(payload, field)
          ? payload.links.filter((item) => item.status === 'active')
          : [];
        const link = activeLinks.find((item) => item.role !== 'source_provenance')
          ?? activeLinks[0];
        setState(link ? { phase: 'ready', linkId: link.stable_id } : { phase: 'empty' });
      },
      () => {
        if (active) setState({ phase: 'error' });
      },
    );
    return () => {
      active = false;
    };
  }, [activeField, api]);

  useEffect(() => {
    onVisibilityChange?.(state.phase === 'ready' || sourcePdf !== null);
  }, [onVisibilityChange, sourcePdf, state.phase]);

  if (state.phase === 'ready' && activeField) {
    return (
      <div className="review-source-preview" data-testid="review-citation-preview">
        <EvidenceViewer
          key={`${activeField.id}:${state.linkId}`}
          evidenceLinkId={state.linkId}
          mode="pane"
          scopeRowId={activeField.rowId}
          highlight
          defaultShowDetails={false}
          onClose={() => {}}
        />
      </div>
    );
  }
  if (sourcePdf) {
    return (
      <div className="review-source-preview">
        {state.phase === 'error' && (
          <p className="review-source-preview-notice muted">Citations unavailable; showing the source PDF.</p>
        )}
        <ReviewSourcePdf name="source" media={sourcePdf} />
      </div>
    );
  }
  return state.phase === 'error'
    ? <p className="review-source-preview-notice muted" role="status">Citations could not be loaded for this result.</p>
    : null;
}

function ReviewSourcePdf({ name, media }: { name: string; media: ResolvedMediaValue }) {
  return (
    <div className="review-source-document" data-testid={`review-source-pdf-${name}`}>
      <DocumentReader
        media={media}
        mediaKind="pdf"
        title={media.filename ?? media.label}
        layout="single"
        fit="page"
        videoFit="full"
        onVideoFitChange={() => {}}
        textLayer={false}
        onPageCount={() => {}}
        rowKey={`review-source:${name}:${media.url}`}
        onOpenDetail={() => {}}
        canOpenDetail={false}
        optionsOpen={false}
        onToggleOptions={() => {}}
        optionsPopover={null}
        selectionCount={0}
      />
    </div>
  );
}
