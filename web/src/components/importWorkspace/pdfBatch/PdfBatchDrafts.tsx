import { Button } from '../../Button';
import { FileText, LoaderCircle, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';

import { pdfImportsApi, type PdfImportSnapshot, type PdfImportsApi } from '../../../api/pdfImports';
import { bytesLabel, count } from './model';
import './PdfBatchImportFlow.css';

export interface PdfBatchDraftsProps {
  projectId: string;
  onResume(draftId: string): void;
  api?: Pick<PdfImportsApi, 'list' | 'discard'>;
}

function activeDraft(snapshot: PdfImportSnapshot): boolean {
  return snapshot.state === 'open' || snapshot.state === 'error' || snapshot.state === 'cancelling';
}

export function PdfBatchDrafts({ projectId, onResume, api = pdfImportsApi }: PdfBatchDraftsProps) {
  const [drafts, setDrafts] = useState<PdfImportSnapshot[]>([]);
  const [loadedProjectId, setLoadedProjectId] = useState<string | null>(null);
  const [discardId, setDiscardId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void api.list(projectId, { signal: controller.signal }).then((items) => {
      if (!controller.signal.aborted) setDrafts(items.filter(activeDraft));
    }).catch((problem: unknown) => {
      if (!controller.signal.aborted) setError(problem instanceof Error ? problem.message : String(problem));
    }).finally(() => {
      if (!controller.signal.aborted) setLoadedProjectId(projectId);
    });
    return () => controller.abort();
  }, [api, projectId]);

  const discard = async (draft: PdfImportSnapshot) => {
    if (busyId) return;
    setBusyId(draft.draft_id);
    setError(null);
    try {
      const next = await api.discard(projectId, draft.draft_id);
      setDrafts((current) => next.state === 'cancelled'
        ? current.filter((item) => item.draft_id !== draft.draft_id)
        : current.map((item) => item.draft_id === draft.draft_id ? next : item));
      setDiscardId(null);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
    } finally {
      setBusyId(null);
    }
  };

  if (loadedProjectId !== projectId) return <div className="pdf-batch-drafts-loading" role="status"><LoaderCircle className="spin" size={14} /> Checking unfinished file imports…</div>;
  if (drafts.length === 0 && !error) return null;

  return <section className="pdf-batch-drafts" aria-label="Unfinished file imports">
    <header><strong>Unfinished file imports</strong><span>Continue without starting over.</span></header>
    {drafts.map((draft) => {
      const total = draft.upload.files_total;
      const copied = draft.upload.files_done;
      const confirming = discardId === draft.draft_id;
      return <article key={draft.draft_id}>
        <FileText size={17} aria-hidden />
        <div><strong>{draft.source_label || count(total, 'file')}</strong><span>{draft.state === 'cancelling'
          ? `Removing ${bytesLabel(draft.cleanup.bytes_done)} of ${bytesLabel(draft.cleanup.bytes_total ?? 0)}`
          : `${copied.toLocaleString()} of ${total.toLocaleString()} copied${draft.paused ? ' · Paused' : ''}`}</span></div>
        {draft.state === 'cancelling' ? <LoaderCircle className="spin" size={15} aria-label="Removing import" /> : confirming ? <div className="pdf-batch-draft-confirm">
          <span>Discard this import?</span>
          <Button type="button" onClick={() => setDiscardId(null)}>Keep</Button>
          <Button type="button" className="btn-danger" disabled={busyId === draft.draft_id} onClick={() => void discard(draft)}>Discard draft</Button>
        </div> : <div className="pdf-batch-draft-actions">
          <Button type="button" onClick={() => onResume(draft.draft_id)}>Resume</Button>
          <button type="button" className="icon-btn" aria-label={`Discard ${draft.source_label || 'file import'}`} onClick={() => setDiscardId(draft.draft_id)}><Trash2 size={14} /></button>
        </div>}
      </article>;
    })}
    {error && <p role="alert">{error}</p>}
  </section>;
}
