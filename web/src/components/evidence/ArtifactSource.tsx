import './EvidenceReading.css';
import { ExternalLink, FileText } from 'lucide-react';
import type { EvidenceArtifact, EvidenceRegion, EvidenceTextPassageTarget } from '../../api/open';
import { EvidenceDocumentSource } from './EvidenceDocumentSource';
import { EvidenceMediaSource } from './EvidenceMediaSource';
import { RegionOverlay } from './EvidenceRegionOverlay';
import { TextArtifactSource, TextPassageRecovery, type RecoveryState } from './EvidenceTextSource';
import { isTextRenderableMediaType, jsonRecord, textCitedSpans } from './evidenceSourceModel';

export function ArtifactSource({
  artifact,
  initialPage,
  textPassageTargets,
  playFocusedPassage,
  scopeSpanId,
  scopeSpanIds,
  highlight = true,
  emphasizedSpanIds = [],
  recovery = { phase: 'idle' },
  onLocateCurrent,
}: {
  artifact: EvidenceArtifact;
  initialPage?: number;
  textPassageTargets?: readonly EvidenceTextPassageTarget[];
  playFocusedPassage?: boolean;
  scopeSpanId?: string;
  /** Matching spans to highlight alongside the primary focused span. */
  scopeSpanIds?: readonly string[];
  highlight?: boolean;
  /** A host may distinguish its focused citation while retaining all support. */
  emphasizedSpanIds?: readonly string[];
  recovery?: RecoveryState;
  onLocateCurrent?: () => void;
}) {
  const blob = artifact.artifact_ref.blob;
  return (
    <section className="evidence-source-section" data-testid="evidence-artifact">
      <header className="evidence-artifact-title">
        <FileText size={15} />
        <div>
          <strong>{artifact.title || artifact.filename || artifact.media_type}</strong>
          <span>{artifact.media_type}</span>
        </div>
        {blob && (
          <a className="btn" href={blob.url} target="_blank" rel="noopener noreferrer">
            <ExternalLink size={12} /> source
          </a>
        )}
      </header>
      <TextPassageRecovery
        artifact={artifact}
        recovery={recovery}
        onLocateCurrent={onLocateCurrent}
      />
      {(artifact.media_type === 'application/pdf' && blob) || artifact.pages.length > 0 ? (
        <EvidenceDocumentSource artifact={artifact} initialPage={initialPage} scopeSpanId={scopeSpanId} emphasizedSpanIds={emphasizedSpanIds} />
      ) : isTextRenderableMediaType(artifact.media_type) && (artifact.text_context != null || textCitedSpans(artifact).length > 0) ? (
        <TextArtifactSource key={artifact.stable_id} artifact={artifact} textPassageTargets={textPassageTargets} playFocusedPassage={playFocusedPassage} scopeSpanId={scopeSpanId} scopeSpanIds={scopeSpanIds} highlight={highlight} emphasizedSpanIds={emphasizedSpanIds} />
      ) : artifact.media_type.startsWith('image/') && blob ? (
        <div className="evidence-page-image-frame">
          <img className="evidence-page-image" src={blob.url} alt={artifact.title || artifact.filename || 'Source image'} />
          {artifact.spans.map((span) => <RegionOverlay key={span.stable_id}
            region={{ id: span.id, stable_id: span.stable_id, bbox: (jsonRecord(span.selector)?.bbox ?? null) as EvidenceRegion['bbox'], snippet: span.quote || span.snippet, raw: span.raw }}
            emphasized={emphasizedSpanIds.includes(span.stable_id)} />)}
        </div>
      ) : (
        <EvidenceMediaSource key={`${artifact.stable_id}:${scopeSpanId ?? ''}`} artifact={artifact} scopeSpanId={scopeSpanId} emphasizedSpanIds={emphasizedSpanIds} />
      )}
    </section>
  );
}
