import { useEffect, useState } from 'react';
import { type CellEvidencePayload, type CellValue, type ReviewBundleField } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { resolveMediaValue, type ResolvedMediaValue } from '../../media/resolveMediaValue';
import { DocumentReader } from '../../workbench/DocumentReader';
import { documentMediaKind } from '../../workbench/documentMedia';
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
  const [state, setState] = useState<ReviewEvidencePreviewState>({ phase: 'empty' });

  useEffect(() => {
    const field = activeField;
    if (!field) {
      setState({ phase: 'empty' });
      return;
    }
    let active = true;
    setState({ phase: 'loading' });
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
  return null;
}

/** A review source is only opened as a document when it is an admitted blob
 * envelope or an absolute PDF URL. A filename-shaped text value is evidence,
 * not a browser navigation target. */
export function reviewPdfMedia(value: CellValue, projectId: string): ResolvedMediaValue | null {
  const raw = typeof value === 'string' ? value : null;
  if (!raw) return null;
  const isBlobEnvelope = (() => {
    if (!raw.trimStart().startsWith('{')) return false;
    try {
      const envelope = JSON.parse(raw) as { blob?: unknown };
      return typeof envelope.blob === 'string' && /^[a-f0-9]{64}$/i.test(envelope.blob);
    } catch {
      return false;
    }
  })();
  const isPdfUrl = (() => {
    try {
      const url = new URL(raw);
      return (url.protocol === 'https:' || url.protocol === 'http:')
        && url.pathname.toLowerCase().endsWith('.pdf');
    } catch {
      return false;
    }
  })();
  if (!isBlobEnvelope && !isPdfUrl) return null;
  const media = resolveMediaValue(raw, projectId);
  return documentMediaKind(media, 'file') === 'pdf' ? media : null;
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
