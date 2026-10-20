import './EvidenceViewer.css';
import { ExternalLink } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { EvidenceViewerPayload } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { PanelLoading } from './PanelPrimitives';
import { WindowCloseButton } from './WindowCloseButton';
import { ArtifactSource } from './evidence/ArtifactSource';
import { ArtifactDetails, GroundingDegradedNotice, LinkSummary } from './evidence/EvidenceDetails';
import { PdfPacketSourceCard } from './evidence/EvidenceDocumentSource';
import type { RecoveryState } from './evidence/EvidenceTextSource';
import { selectorNumber } from './evidence/evidenceSourceModel';
import { pdfPacketSourceCardData } from './evidence/pdfPacketSourceCardModel';

export interface EvidenceViewerProps {
  evidenceLinkId: string | number;
  mode?: 'peek' | 'pane';
  onClose(): void;
  onOpenCompanion?(): void;
  /** A no-match row scope falls back to all source artifacts. */
  scopeRowId?: string | null;
  scopeSpanId?: string;
  /** Matching spans to highlight alongside the primary focused span. */
  scopeSpanIds?: readonly string[];
  highlight?: boolean;
  defaultShowDetails?: boolean;
}

type ViewerState =
  | { phase: 'loading' }
  | { phase: 'error'; message: string }
  | { phase: 'done'; payload: EvidenceViewerPayload };

export function EvidenceViewer({
  evidenceLinkId,
  mode = 'peek',
  onClose,
  onOpenCompanion,
  scopeRowId = null,
  scopeSpanId,
  scopeSpanIds,
  highlight = true,
  defaultShowDetails = false,
}: EvidenceViewerProps) {
  const { projectApi, route } = useWorkspaceStores();
  const [state, setState] = useState<ViewerState>({ phase: 'loading' });
  const [recovery, setRecovery] = useState<RecoveryState>({ phase: 'idle' });
  const requestEpoch = useRef(0);
  const mounted = useRef(true);
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      requestEpoch.current += 1;
    };
  }, []);

  useEffect(() => {
    const epoch = ++requestEpoch.current;
    let alive = true;
    projectApi
      .getEvidenceViewer(evidenceLinkId)
      .then((payload) => {
        if (alive && requestEpoch.current === epoch) {
          setState({ phase: 'done', payload });
          setRecovery({ phase: 'idle' });
        }
      })
      .catch((error: Error) => {
        if (alive && requestEpoch.current === epoch) {
          setState({ phase: 'error', message: error.message });
          setRecovery({ phase: 'idle' });
        }
      });
    return () => {
      alive = false;
    };
  }, [evidenceLinkId, projectApi]);

  const locateCurrentPassage = () => {
    if (state.phase !== 'done' || recovery.phase === 'loading') return;
    const epoch = ++requestEpoch.current;
    setRecovery({ phase: 'loading' });
    projectApi
      .getEvidenceViewer(evidenceLinkId, { locateCurrent: true })
      .then((payload) => {
        if (!mounted.current || requestEpoch.current !== epoch) return;
        setState({ phase: 'done', payload });
        setRecovery({ phase: 'idle' });
      })
      .catch((error: Error) => {
        if (!mounted.current || requestEpoch.current !== epoch) return;
        setRecovery({
          phase: 'error',
          message: error.message || 'Could not search the current source.',
        });
      });
  };

  useEffect(() => {
    if (mode !== 'peek') return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
    return () => {
      if (dialog.open) dialog.close();
    };
  }, [mode]);

  const content = (
    <>
      <EvidenceViewerHeader
        state={state}
        canOpenCompanion={mode === 'peek' && onOpenCompanion !== undefined}
        onOpenCompanion={onOpenCompanion}
        onClose={onClose}
      />
      {state.phase === 'loading' && (
        <PanelLoading testId="evidence-viewer-loading" label="Loading evidence…" />
      )}
      {state.phase === 'error' && (
        <div className="panel-notice" data-tone="warning" data-testid="evidence-viewer-error">
          Could not load evidence: {state.message}
        </div>
      )}
      {state.phase === 'done' && (
        <EvidencePayloadView
          payload={state.payload}
          scopeRowId={scopeRowId}
          scopeSpanId={scopeSpanId}
          scopeSpanIds={scopeSpanIds}
          highlight={highlight}
          defaultShowDetails={defaultShowDetails}
          recovery={recovery}
          onLocateCurrent={locateCurrentPassage}
          onOpenSiblingSheet={(sheetId) => {
            const current = route.store.get();
            route.navigate({
              ...current,
              sheetId: String(sheetId),
              actionKind: null,
              review: false,
              panel: null,
            });
            onClose();
          }}
        />
      )}
    </>
  );

  if (mode === 'pane') {
    return (
      <section className="evidence-viewer-shell evidence-viewer-pane" data-testid="evidence-viewer" aria-label="Evidence viewer">
        {content}
      </section>
    );
  }

  return (
    <div className="evidence-viewer-backdrop" data-testid="evidence-viewer">
      <dialog
        ref={dialogRef}
        className="evidence-viewer-shell"
        aria-label="Evidence viewer"
        onCancel={(event) => {
          event.preventDefault();
          onClose();
        }}
      >
        {content}
      </dialog>
    </div>
  );
}

function EvidenceViewerHeader({
  state,
  canOpenCompanion,
  onOpenCompanion,
  onClose,
}: {
  state: ViewerState;
  canOpenCompanion: boolean;
  onOpenCompanion?: () => void;
  onClose(): void;
}) {
  const sourceProvenance = state.phase === 'done' && state.payload.link.role === 'source_provenance';
  return (
    <header className="evidence-viewer-header">
      <div>
        <h2>{sourceProvenance ? 'Source provenance' : 'Evidence'}</h2>
        {state.phase === 'done' && state.payload.link.status !== 'active' && (
          <div className="evidence-viewer-subtitle">
            {state.payload.link.status === 'stale' ? 'Source may have changed' : state.payload.link.status}
          </div>
        )}
      </div>
      <div className="control-row">
        {canOpenCompanion && (
          <button
            type="button"
            className="icon-btn"
            aria-label="Open evidence beside grid"
            title="Open evidence beside grid"
            onClick={onOpenCompanion}
          >
            <ExternalLink size={16} />
          </button>
        )}
        <WindowCloseButton label="Close evidence viewer" onClick={onClose} />
      </div>
    </header>
  );
}

function EvidencePayloadView({
  payload,
  scopeRowId = null,
  scopeSpanId,
  scopeSpanIds,
  highlight = true,
  defaultShowDetails = false,
  recovery,
  onLocateCurrent,
  onOpenSiblingSheet,
}: {
  payload: EvidenceViewerPayload;
  scopeRowId?: string | null;
  scopeSpanId?: string;
  /** Matching spans to highlight alongside the primary focused span. */
  scopeSpanIds?: readonly string[];
  highlight?: boolean;
  defaultShowDetails?: boolean;
  recovery: RecoveryState;
  onLocateCurrent(): void;
  onOpenSiblingSheet(sheetId: number): void;
}) {
  const focusedArtifacts = useMemo(() => {
    const selectedIds = new Set(scopeSpanIds);
    if (scopeSpanId) selectedIds.add(scopeSpanId);
    if (selectedIds.size === 0) return payload.artifacts;
    return payload.artifacts.filter((artifact) => artifact.spans.some((span) => selectedIds.has(span.stable_id))).map((artifact) => {
      const selectedSpans = artifact.spans.filter((span) => selectedIds.has(span.stable_id));
      return {
        ...artifact,
        spans: highlight ? selectedSpans : [],
        runs: highlight ? artifact.runs.filter((run) => run.span_ids.some((id) => selectedIds.has(id))) : [],
        pages: artifact.pages.filter((page) => !highlight || selectedSpans.some((span) => {
          const first = selectorNumber(span, 'page_start') ?? selectorNumber(span, 'page');
          const last = selectorNumber(span, 'page_end') ?? first;
          return first === null || (page.page >= first && page.page <= (last ?? first));
        })).map((page) => ({
          ...page,
          regions: highlight ? page.regions.filter((region) => selectedIds.has(region.stable_id)) : [],
        })),
      };
    });
  }, [payload.artifacts, scopeSpanId, scopeSpanIds, highlight]);
  const orderedArtifacts = useMemo(() => {
    if (scopeRowId === null) return focusedArtifacts;
    const scoped = focusedArtifacts.filter(
      (artifact) => String(artifact.source_cell?.row_id ?? '') === scopeRowId,
    );
    return scoped.length > 0 ? scoped : focusedArtifacts;
  }, [focusedArtifacts, scopeRowId]);
  const [showDetails, setShowDetails] = useState(defaultShowDetails);
  const packetSource = payload.link.role === 'source_provenance'
    ? orderedArtifacts.map((artifact) => ({ artifact, data: pdfPacketSourceCardData(artifact) }))
      .find((candidate) => candidate.data !== null) ?? null
    : null;
  return (
    <div
      className={`evidence-viewer-content${showDetails ? '' : ' evidence-viewer-content-compact'}`}
    >
      <main className="evidence-viewer-source-pane">
        {packetSource?.data ? (
          <PdfPacketSourceCard
            data={packetSource.data}
            splitAt={payload.link.created_at}
            onOpenSiblingSheet={onOpenSiblingSheet}
          />
        ) : payload.link.role === 'source_provenance' && (
          <div className="panel-notice" data-tone="warning" data-testid="evidence-provenance-notice">
            Original document used to create this text.
          </div>
        )}
        {orderedArtifacts.length === 0 ? (
          <div className="panel-notice" data-tone="warning" data-testid="evidence-viewer-empty">
            This evidence link has no source spans.
          </div>
        ) : (
          orderedArtifacts.map((artifact) => (
            pdfPacketSourceCardData(artifact) ? null : (
              <ArtifactSource
                key={`${artifact.stable_id}:${scopeSpanId ?? ''}:${highlight}`}
                artifact={artifact}
                scopeSpanId={scopeSpanId}
                scopeSpanIds={scopeSpanIds}
                highlight={highlight}
                recovery={recovery}
                onLocateCurrent={onLocateCurrent}
              />
            )
          ))
        )}
        <button
          type="button"
          className="evidence-details-toggle"
          data-testid="evidence-details-toggle"
          onClick={() => setShowDetails((value) => !value)}
        >
          {showDetails ? 'Hide details' : 'Show details'}
        </button>
      </main>
      {showDetails && (
        <aside className="evidence-viewer-detail-pane" data-testid="evidence-viewer-detail-pane">
          <LinkSummary payload={payload} />
          <GroundingDegradedNotice
            warnings={payload.warnings}
            hasSavedTextContext={orderedArtifacts.some((artifact) => artifact.text_context != null)}
          />
          {orderedArtifacts.map((artifact) => (
            <ArtifactDetails key={artifact.stable_id} artifact={artifact} />
          ))}
        </aside>
      )}
    </div>
  );
}
