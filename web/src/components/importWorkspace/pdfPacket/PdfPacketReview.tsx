import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState, type ReactNode } from 'react';
import { Button } from '../../Button';
import { ProgressBar } from '../../ActivityProgress';
import { WindowCloseButton } from '../../WindowCloseButton';
import { DiscardImportConfirm } from '../pdfBatch/DiscardImportConfirm';
import { count } from '../pdfBatch/model';
import {
  cancelPdfPacketOcr, getPdfPacketSplit, matchPdfPacketCandidates, setPdfPacketTextSource,
  type PdfPacketSplitSnapshot,
} from '../../../api/pdfPacketSplits';
import { initialPdfPacketFlowState, pdfPacketFlowReducer } from './model';
import { PacketFindStep } from './PacketFindStep';
import { createReviewSources, reviewedStarts, type ReviewSource } from './reviewSources';

type CombinedSnapshot = PdfPacketSplitSnapshot & { sources: ReviewSource[]; fixed_starts: number[]; example_pages: number[] };

/** Shared review for an admitted draft or sheet scope; publication remains with the caller. */
export function PdfPacketReview({ projectId, snapshot: initial, onReviewed, onBack, onClose, registerCloseRequest, headerContent, headerSubtitle, primaryLabel, discardText, onSnapshotChange }: {
  projectId: string;
  snapshot: PdfPacketSplitSnapshot;
  onReviewed(starts: Record<string, number[]>): void | Promise<void>;
  onBack(): void;
  onClose?(): void;
  registerCloseRequest?(handler: () => void): () => void;
  headerContent?: ReactNode;
  headerSubtitle?: string;
  primaryLabel?: string | ((documents: number) => string);
  discardText?: string;
  onSnapshotChange?(snapshot: PdfPacketSplitSnapshot): void;
}) {
  const [state, dispatch] = useReducer(pdfPacketFlowReducer, {
    ...pdfPacketFlowReducer(initialPdfPacketFlowState, { type: 'prepared', snapshot: initial }),
  });
  const snapshot = state.snapshot as CombinedSnapshot;
  const sources = useMemo(() => createReviewSources(snapshot.sources), [snapshot.sources]);
  const pageCount = sources.reduce((sum, source) => sum + source.page_end - source.page_start + 1, 0);
  const revisionRef = useRef(snapshot.analysis_revision);
  useEffect(() => { revisionRef.current = snapshot.analysis_revision; }, [snapshot.analysis_revision]);
  const [busy, setBusy] = useState(false);
  const initialExamplePages = (initial as CombinedSnapshot).example_pages ?? [];
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [discardTarget, setDiscardTarget] = useState<'back' | 'close' | null>(null);

  useEffect(() => { onSnapshotChange?.(snapshot); }, [snapshot, onSnapshotChange]);
  useEffect(() => {
    if (snapshot.status !== 'preparing' && !snapshot.jobs.some((job) => job.progress.status === 'queued' || job.progress.status === 'running')) return undefined;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void getPdfPacketSplit(projectId, snapshot.split_id, { signal: controller.signal })
        .then((next) => { if (!controller.signal.aborted) dispatch({ type: 'snapshot', snapshot: next }); })
        .catch((error: unknown) => { if (!controller.signal.aborted) dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) }); });
    }, 1000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [projectId, snapshot]);
  useEffect(() => {
    if (snapshot.status !== 'ready' || snapshot.text_source !== 'unconfirmed') return undefined;
    const controller = new AbortController();
    void setPdfPacketTextSource(projectId, snapshot.split_id, { kind: 'native' }, { signal: controller.signal })
      .then((next) => { if (!controller.signal.aborted) dispatch({ type: 'snapshot', snapshot: next }); })
      .catch((error: unknown) => { if (!controller.signal.aborted) dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) }); });
    return () => controller.abort();
  }, [projectId, snapshot.split_id, snapshot.status, snapshot.text_source]);
  useEffect(() => {
    if (snapshot.status !== 'ready' || snapshot.text_source === 'unconfirmed' || state.method === 'manual') return undefined;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      dispatch({ type: 'matching' });
      void matchPdfPacketCandidates(projectId, snapshot.split_id, {
        expected_analysis_revision: snapshot.analysis_revision,
        confirmed_starts: state.confirmedStarts,
        example_pages: [...new Set([...state.examplePages, ...state.confirmedStarts.filter((page) => !state.fixedStarts.includes(page))])],
        rejected: state.rejected, threshold_pct: state.threshold,
        phrases: state.method === 'text' ? state.phrases.filter((phrase) => phrase.text.trim()).map((phrase) => ({ ...phrase, text: phrase.text.trim() })) : [],
      }, { signal: controller.signal }).then((result) => {
        if (!controller.signal.aborted && result.analysis_revision === revisionRef.current) dispatch({ type: 'matches', value: result });
      }).catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if ((error as { status?: number }).status === 409) {
          void getPdfPacketSplit(projectId, snapshot.split_id, { signal: controller.signal }).then((next) => { if (!controller.signal.aborted) dispatch({ type: 'snapshot', snapshot: next }); }).catch((problem: unknown) => { if (!controller.signal.aborted) dispatch({ type: 'error', message: problem instanceof Error ? problem.message : String(problem) }); });
        } else dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) });
      });
    }, 180);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [projectId, snapshot.split_id, snapshot.status, snapshot.text_source, snapshot.analysis_revision, state.method, state.confirmedStarts, state.fixedStarts, state.examplePages, state.rejected, state.threshold, state.phrases]);

  const continueReview = async () => {
    setBusy(true);
    try { await onReviewed(reviewedStarts(sources, state.confirmedStarts)); }
    catch (error) { dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) }); }
    finally { setBusy(false); }
  };
  const cancelOcr = (jobId: string) => {
    void cancelPdfPacketOcr(projectId, snapshot.split_id, jobId).then((next) => dispatch({ type: 'snapshot', snapshot: next }))
      .catch((error: unknown) => dispatch({ type: 'error', message: error instanceof Error ? error.message : String(error) }));
  };
  const requestExit = useCallback((target: 'back' | 'close') => {
    if (discardTarget) { setDiscardTarget(null); window.requestAnimationFrame(() => closeButtonRef.current?.focus()); return; }
    if (state.dirty) setDiscardTarget(target);
    else if (target === 'close') onClose?.();
    else onBack();
  }, [discardTarget, state.dirty, onClose, onBack]);
  useLayoutEffect(() => registerCloseRequest?.(() => requestExit('close')), [registerCloseRequest, requestExit]);
  return <div className="pdf-batch-flow pdf-packet-review-host">
    {onClose && <header className="import-workspace-header"><div><div className="import-workspace-title" role="heading" aria-level={2}>Split PDFs</div>{headerSubtitle && <div className="import-workspace-kicker">{headerSubtitle}</div>}</div>
      {headerContent}
      <WindowCloseButton ref={closeButtonRef} label="Close split PDFs" onClick={() => requestExit('close')} />
    </header>}
    {!onClose && headerContent}
    {snapshot.status === 'preparing' ? <div className="pdf-batch-loading" role="status">
      <strong>Preparing pages</strong>
      <span>{snapshot.prepare.progress.done} of {snapshot.prepare.progress.total ?? pageCount} pages</span>
      <ProgressBar label="Preparing pages" completed={snapshot.prepare.progress.done} total={snapshot.prepare.progress.total ?? pageCount} state="active" />
      <Button type="button" onClick={() => requestExit('back')}>Back</Button>
    </div> : snapshot.status === 'ready' ? <PacketFindStep
      projectId={projectId} splitId={snapshot.split_id} state={state} pageCount={pageCount} dispatch={dispatch} sources={sources}
      onCancelOcr={cancelOcr} onBack={() => requestExit('back')} onContinue={() => void continueReview()}
      primaryLabel={primaryLabel} continueDisabled={busy} /> : <div className="pdf-batch-loading">
      <p role="alert">{snapshot.prepare.progress.error ?? 'Could not prepare these PDFs.'}</p>
      <Button type="button" onClick={() => requestExit('back')}>Back</Button>
    </div>}
    {state.error && <p role="alert">{state.error}</p>}
    {discardTarget && <DiscardImportConfirm title="Discard this split?" testId="pdf-split-discard-dialog" confirmLabel="Discard"
      message={`${count(state.confirmedStarts.length - state.fixedStarts.length + state.rejected.length + state.examplePages.filter((page) => state.fixedStarts.includes(page) && !initialExamplePages.includes(page)).length, 'start')} you marked or answered will be lost. ${discardText ?? 'Text read so far stays in this import.'}`}
      onKeep={() => { setDiscardTarget(null); window.requestAnimationFrame(() => closeButtonRef.current?.focus()); }}
      onConfirm={() => {
        const target = discardTarget;
        setDiscardTarget(null);
        dispatch({ type: 'prepared', snapshot });
        if (target === 'close') onClose?.();
        else onBack();
      }} />}

  </div>;
}
