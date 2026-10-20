import { useEffect, useState } from 'react';
import type { CellEvidencePayload } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import './CellEvidence.css';
import { Button } from '../Button';
import { CitationChip } from '../evidence/CitationChip';

type EvidenceBlockState =
  | { phase: 'loading' | 'empty' | 'error'; payload?: null }
  | { phase: 'ready'; payload: CellEvidencePayload; auditOpen: boolean };

export function CellEvidenceBlock({
  rowId,
  columnId,
  columnName,
}: {
  rowId: string;
  columnId: string;
  columnName: string;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const [state, setState] = useState<EvidenceBlockState>({ phase: 'loading' });

  useEffect(() => {
    let alive = true;
    api
      .getCellEvidence(rowId, columnId)
      .then((payload) => {
        if (!alive) return;
        if (payload.links.length === 0 && payload.stale_count === 0) {
          setState({ phase: 'empty' });
        } else {
          setState({ phase: 'ready', payload, auditOpen: false });
        }
      })
      .catch(() => {
        if (alive) setState({ phase: 'error' });
      });
    return () => {
      alive = false;
    };
  }, [api, rowId, columnId]);

  if (state.phase === 'error') {
    return (
      <div className="cell-evidence cell-evidence-error" data-testid={`cell-evidence-error-${columnName}`}>
        Evidence unavailable
      </div>
    );
  }
  if (state.phase !== 'ready') return null;

  const openAudit = () => {
    if (state.auditOpen) {
      setState((current) => (
        current.phase === 'ready' ? { ...current, auditOpen: false } : current
      ));
      return;
    }
    api
      .getCellEvidence(rowId, columnId, { includeStale: true })
      .then((payload) => setState({ phase: 'ready', payload, auditOpen: true }))
      .catch(() => setState((current) => (
        current.phase === 'ready' ? { ...current, auditOpen: true } : current
      )));
  };
  const activeLinks = state.payload.links.filter((link) => link.status === 'active');
  const staleLinks = state.payload.links.filter((link) => link.status === 'stale');
  return (
    <div className="cell-evidence" data-testid={`cell-evidence-${columnName}`}>
      {activeLinks.length > 0 && (
        <div className="cell-evidence-links" data-testid={`cell-evidence-active-${columnName}`}>
          {activeLinks.map((link) => (
            <CitationChip
              key={link.stable_id}
              linkId={link.stable_id}
              label="Evidence"
              snippet={link.snippet}
              testId={`cell-evidence-open-${columnName}`}
            />
          ))}
        </div>
      )}
      {state.payload.stale_count > 0 && (
        <Button
          type="button"
          className="cell-evidence-audit-toggle"
          data-testid={`cell-evidence-stale-toggle-${columnName}`}
          onClick={openAudit}
        >
          stale evidence ({state.payload.stale_count})
        </Button>
      )}
      {state.auditOpen && staleLinks.length > 0 && (
        <div className="cell-evidence-stale-list" data-testid={`cell-evidence-stale-${columnName}`}>
          {staleLinks.map((link) => (
            <CitationChip
              key={link.stable_id}
              linkId={link.stable_id}
              label="Stale evidence"
              snippet={link.snippet}
              className="cell-evidence-link-stale"
              testId={`cell-evidence-open-stale-${columnName}`}
            />
          ))}
        </div>
      )}
    </div>
  );
}
