import { FileText } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { RowSourceDocumentsPayload } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { AnchoredMenu } from '../MenuPop';
import { CitationChip } from '../evidence/CitationChip';
import './RowSourceDocuments.css';

type SourceState =
  | { phase: 'loading' }
  | { phase: 'error'; rowId: string }
  | { phase: 'ready'; rowId: string; payload: RowSourceDocumentsPayload };

export function RowSourceDocuments({
  rowId,
  onOpenDocument,
}: {
  rowId: string;
  onOpenDocument(sheetId: string, rowId: string, columnId: string): void;
}) {
  const { projectApi } = useWorkspaceStores();
  const [state, setState] = useState<SourceState>({ phase: 'loading' });
  const [menuOpen, setMenuOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let alive = true;
    projectApi.getRowSourceDocuments(rowId)
      .then((payload) => {
        if (alive) setState({ phase: 'ready', rowId, payload });
      })
      .catch(() => {
        if (alive) setState({ phase: 'error', rowId });
      });
    return () => { alive = false; };
  }, [projectApi, rowId]);

  if (state.phase === 'loading' || state.rowId !== rowId) return null;
  if (state.phase === 'error') {
    return <p className="row-source-note" data-testid="row-source-documents-error">Source documents unavailable</p>;
  }
  const { payload } = state;
  if (!payload.ancestry_found && payload.evidence_links.length === 0 && !payload.lineage_stale) return null;

  const openDocument = (document: RowSourceDocumentsPayload['documents'][number]) => {
    setMenuOpen(false);
    onOpenDocument(
      String(document.sheet_id),
      String(document.row_id),
      String(document.column_id),
    );
  };
  const documentLabel = (document: RowSourceDocumentsPayload['documents'][number]) => (
    `${document.filename ?? document.column_name}${document.page ? ` · Page ${document.page}` : ''}`
      + ` · ${document.sheet_name} · Row ${document.row_position}`
  );

  return (
    <section className="row-source-documents" data-testid="row-source-documents" aria-label="Source documents">
      <h3>Source documents</h3>
      {payload.lineage_stale && (
        <p className="row-source-note" data-testid="row-source-documents-stale">
          This table is out of date; some source links may be missing. Refresh it to update them.
        </p>
      )}
      {payload.evidence_links.length > 0 && (
        <div className="row-source-evidence" data-testid="row-source-evidence">
          {payload.evidence_links.map((link) => (
            <CitationChip
              key={link.stable_id}
              linkId={link.stable_id}
              label="Open evidence"
              snippet={link.snippet}
              testId="row-source-evidence-open"
            />
          ))}
        </div>
      )}
      {payload.documents.length === 1 && (
        <button
          type="button"
          className="row-lineage row-open-source-document"
          data-testid="row-open-source-document"
          onClick={() => openDocument(payload.documents[0])}
          title={documentLabel(payload.documents[0])}
        >
          <FileText size={14} aria-hidden /> Open source document
        </button>
      )}
      {payload.documents.length > 1 && (
        <>
          <button
            ref={triggerRef}
            type="button"
            className="row-lineage row-open-source-document"
            data-testid="row-open-source-documents"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <FileText size={14} aria-hidden /> Open source documents ({payload.documents.length})
          </button>
          {menuOpen && (
            <AnchoredMenu
              anchorRef={triggerRef}
              onClose={() => setMenuOpen(false)}
              aria-label="Choose a source document"
              data-testid="row-source-document-menu"
              width={300}
            >
              {payload.documents.map((document) => (
                <button
                  key={`${document.sheet_id}:${document.row_id}:${document.column_id}`}
                  type="button"
                  role="menuitem"
                  className="menu-item"
                  onClick={() => openDocument(document)}
                >
                  {documentLabel(document)}
                </button>
              ))}
            </AnchoredMenu>
          )}
        </>
      )}
      {payload.ancestry_found && payload.documents.length === 0 && payload.evidence_links.length === 0 && (
        <p className="row-source-note" data-testid="row-source-documents-empty">No readable source document found</p>
      )}
      {payload.unavailable_source_count > 0 && (
        <p className="row-source-note" data-testid="row-source-documents-unavailable">
          {payload.unavailable_source_count} source {payload.unavailable_source_count === 1 ? 'row is' : 'rows are'} unavailable
        </p>
      )}
      {payload.truncated && (
        <p className="row-source-note" data-testid="row-source-documents-truncated">
          Some sources aren’t shown.
        </p>
      )}
    </section>
  );
}
