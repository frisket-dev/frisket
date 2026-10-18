import { AlertTriangle, LoaderCircle } from 'lucide-react';
import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';

import {
  pdfImportsApi,
  PdfImportResponseError,
  type PdfImportCategory,
  type PdfImportBatchOcrEstimate,
  type PdfImportChoices,
  type PdfImportDestination,
  type PdfImportFileSummary,
  type PdfImportPage,
  type PdfImportPreviewEstimate,
  type PdfImportSnapshot,
  type PdfImportUpload,
  type PdfImportsApi,
} from '../../../api/pdfImports';
import { usePoll } from '../../../hooks/usePoll';
import { CostGateModal } from '../../CostGateModal';
import {
  PDF_BATCH_STAGES,
  bytesLabel,
  canHandlePdfBatch,
  count,
  destinationIsReady,
  newerSnapshot,
  ocrPolicyCount,
  visibleCategories,
  type PdfBatchStage,
} from './model';
import { AddFilesStep, CheckTextStep, DestinationStep, SearchSetupStep } from './PdfBatchSteps';
import './PdfBatchImportFlow.css';

export interface PdfBatchDestinationSheet {
  id: number;
  name: string;
}

export interface PdfBatchShellContext {
  active: true;
  stage: PdfBatchStage;
  stages: typeof PDF_BATCH_STAGES;
  subtitle: string;
  busy: boolean;
}

export interface PdfBatchImportFlowProps {
  projectId: string;
  files?: readonly File[];
  resumeDraftId?: string | null;
  destinationSheets: readonly PdfBatchDestinationSheet[];
  initialDestinationSheetId?: number | null;
  api?: PdfImportsApi;
  onContextChange(context: PdfBatchShellContext): void;
  registerCloseGuard(guard: (() => boolean) | null): void;
  onImported(sheetId: number): void;
  onDiscarded(): void;
  onError(message: string): void;
}

interface PendingPreviewConsent {
  estimate: PdfImportPreviewEstimate;
  fileId: string;
  page: number;
}

const CHECK_CATEGORY_ORDER: readonly PdfImportCategory[] = [
  'suspicious', 'missing', 'text', 'image', 'other', 'unreadable',
];

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function subtitle(snapshot: PdfImportSnapshot | null, files: readonly File[]): string {
  const total = snapshot?.upload.files_total ?? files.length;
  const totalBytes = snapshot?.upload.bytes_total
    ?? files.reduce((sum, file) => sum + file.size, 0);
  return `${count(total, 'PDF')} · ${bytesLabel(totalBytes)}`;
}

export function PdfBatchImportFlow({
  projectId,
  files = [],
  resumeDraftId = null,
  destinationSheets,
  initialDestinationSheetId = null,
  api = pdfImportsApi,
  onContextChange,
  registerCloseGuard,
  onImported,
  onDiscarded,
  onError,
}: PdfBatchImportFlowProps) {
  const [snapshot, setSnapshot] = useState<PdfImportSnapshot | null>(null);
  const [stage, setStage] = useState<PdfBatchStage>('add');
  const [error, setError] = useState<string | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<PdfImportCategory>('suspicious');
  const [sampleFiles, setSampleFiles] = useState<PdfImportFileSummary[]>([]);
  const [sampleCursor, setSampleCursor] = useState<string | null>(null);
  const [selectedFileId, setSelectedFileId] = useState<string | null>(null);
  const [page, setPage] = useState<PdfImportPage | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [textSource, setTextSource] = useState<'native' | 'ocr'>('native');
  const [previewStarting, setPreviewStarting] = useState(false);
  const [previewConsent, setPreviewConsent] = useState<PendingPreviewConsent | null>(null);
  const [batchOcrConsent, setBatchOcrConsent] = useState<PdfImportBatchOcrEstimate | null>(null);
  const [batchEstimateBusy, setBatchEstimateBusy] = useState(false);
  const [discardPrompt, setDiscardPrompt] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [reselecting, setReselecting] = useState(false);
  const [mutationBusy, setMutationBusy] = useState(false);
  const [destination, setDestination] = useState<PdfImportDestination>(() => (
    initialDestinationSheetId == null
      ? { kind: 'new', sheet_name: 'Imported PDFs' }
      : { kind: 'existing', sheet_id: initialDestinationSheetId }
  ));
  const [semanticConsent, setSemanticConsent] = useState(false);
  const [semanticEnabled, setSemanticEnabled] = useState<boolean | null>(null);
  const uploadController = useRef<AbortController | null>(null);
  const liveController = useRef<AbortController | null>(null);
  const commitKey = useRef(crypto.randomUUID());
  const currentDraft = useRef<string | null>(null);
  const generation = useRef(0);
  const importedSheet = useRef<number | null>(null);
  const initialFiles = useRef(files);
  const fileInventory = useRef(files.map((file) => ({
    file,
    fileId: crypto.randomUUID(),
    logicalPath: file.webkitRelativePath || file.name,
    batchId: crypto.randomUUID(),
  })));
  const manifestBatchIds = useRef(
    Array.from({ length: Math.ceil(files.length / 256) }, () => crypto.randomUUID()),
  );
  const committed = useRef(false);

  const publish = useCallback((next: PdfImportSnapshot) => {
    if (currentDraft.current && next.draft_id !== currentDraft.current) return;
    currentDraft.current = next.draft_id;
    setSnapshot((current) => newerSnapshot(current, next));
  }, []);

  const reportError = useCallback((problem: unknown) => {
    const message = errorMessage(problem);
    setError(message);
    onError(message);
  }, [onError]);

  useEffect(() => {
    const epoch = ++generation.current;
    const controller = new AbortController();
    liveController.current?.abort();
    liveController.current = controller;
    currentDraft.current = null;
    importedSheet.current = null;
    committed.current = false;
    const start = async () => {
      if (!resumeDraftId) {
        const invalid = !canHandlePdfBatch(initialFiles.current);
        if (invalid) throw new Error('Choose at least two PDFs for this import.');
      }
      const created = resumeDraftId
        ? await api.get(projectId, resumeDraftId, { signal: controller.signal })
        : await api.create(
          projectId,
          fileInventory.current.length,
          fileInventory.current.reduce((sum, item) => sum + item.file.size, 0),
          fileInventory.current[0]?.logicalPath.includes('/')
            ? fileInventory.current[0].logicalPath.split('/')[0]
            : `${fileInventory.current.length} PDFs`,
          { signal: controller.signal },
        );
      if (controller.signal.aborted || generation.current !== epoch) return;
      publish(created);
      if (!resumeDraftId) {
        for (let start = 0, index = 0; start < fileInventory.current.length; start += 256, index += 1) {
          const manifest = fileInventory.current.slice(start, start + 256).map((item) => ({
            client_file_id: item.fileId,
            logical_path: item.logicalPath,
            filename: item.file.name,
            size: item.file.size,
          }));
          publish(await api.addManifest(
            projectId,
            created.draft_id,
            manifest,
            manifestBatchIds.current[index],
            { signal: controller.signal },
          ));
        }
        publish(await api.sealManifest(projectId, created.draft_id, { signal: controller.signal }));
        const upload = new AbortController();
        uploadController.current = upload;
        try {
          for (const item of fileInventory.current) {
            const uploaded = await api.upload(
              projectId,
              created.draft_id,
              [{ fileId: item.fileId, file: item.file, logicalPath: item.logicalPath }],
              item.batchId,
              { signal: upload.signal },
            );
            if (upload.signal.aborted) break;
            if (generation.current === epoch) publish(uploaded);
          }
        } finally {
          if (uploadController.current === upload) uploadController.current = null;
        }
      }
    };
    void start().catch((problem) => {
      if (!controller.signal.aborted && !(problem instanceof DOMException && problem.name === 'AbortError')) {
        reportError(problem);
      }
    });
    return () => {
      controller.abort();
      if (!committed.current) uploadController.current?.abort();
    };
  }, [api, projectId, publish, reportError, resumeDraftId]);

  const refresh = useCallback(async () => {
    const draftId = currentDraft.current;
    if (!draftId) return;
    try {
      const next = await api.get(projectId, draftId);
      publish(next);
      if (next.state === 'cancelled') onDiscarded();
      const sheetId = next.commit?.result?.sheet_id;
      if (next.state === 'committed' && sheetId && importedSheet.current !== sheetId) {
        committed.current = true;
        importedSheet.current = sheetId;
        onImported(sheetId);
      }
    } catch (problem) {
      reportError(problem);
    }
  }, [api, onDiscarded, onImported, projectId, publish, reportError]);

  usePoll(refresh, {
    active: Boolean(snapshot && (
      snapshot.state === 'committing'
      || snapshot.state === 'cancelling'
      || (!snapshot.paused && snapshot.state === 'open'
        && (!snapshot.upload.sealed || snapshot.analysis.files_done < (snapshot.analysis.files_total ?? Infinity)))
      || snapshot.preview_jobs?.some((job) => job.status === 'queued' || job.status === 'running')
    )),
    intervalMs: 1_200,
    guardOverlap: true,
  });

  const chosenFile = sampleFiles.find((file) => file.occurrence_id === selectedFileId) ?? null;
  const previewJob = snapshot?.preview_jobs?.find((job) => (
    job.occurrence_id === selectedFileId && job.page === 1
  ));

  const loadPage = useCallback(async (file: PdfImportFileSummary) => {
    if (!snapshot) return;
    const controller = new AbortController();
    liveController.current?.abort();
    liveController.current = controller;
    setPageLoading(true);
    try {
      const result = await api.getPage(projectId, snapshot.draft_id, file.occurrence_id, 1, { signal: controller.signal });
      if (!controller.signal.aborted && currentDraft.current === (result.draft_id || result.import_id)) setPage(result);
    } catch (problem) {
      if (!controller.signal.aborted) reportError(problem);
    } finally {
      if (!controller.signal.aborted) setPageLoading(false);
    }
  }, [api, projectId, reportError, snapshot]);

  usePoll(async () => {
    if (!snapshot || !chosenFile) return 'stop';
    const result = await api.getPage(projectId, snapshot.draft_id, chosenFile.occurrence_id, 1);
    setPage(result);
    return result.ocr_text && result.ocr_engine === snapshot.choices.ocr_engine ? 'stop' : undefined;
  }, {
    active: Boolean(previewJob && previewJob.status !== 'failed' && previewJob.status !== 'cancelled'
      && (!page?.ocr_text || page.ocr_engine !== snapshot?.choices.ocr_engine)),
    intervalMs: 700,
    guardOverlap: true,
    immediate: true,
    mode: 'settle-relative',
  });

  useEffect(() => {
    if (!snapshot) return;
    onContextChange({
      active: true,
      stage,
      stages: PDF_BATCH_STAGES,
      subtitle: subtitle(snapshot, initialFiles.current),
      busy: mutationBusy || snapshot.state === 'committing' || snapshot.state === 'cancelling',
    });
  }, [mutationBusy, onContextChange, snapshot, stage]);

  const pauseForDiscard = useCallback(() => {
    if (!snapshot || snapshot.state !== 'open' || discardPrompt) return;
    setDiscardPrompt(true);
    void api.pause(projectId, snapshot.draft_id).then(publish).catch(reportError);
  }, [api, discardPrompt, projectId, publish, reportError, snapshot]);

  useEffect(() => {
    registerCloseGuard(snapshot && snapshot.state === 'open' ? () => {
      pauseForDiscard();
      return false;
    } : null);
    return () => registerCloseGuard(null);
  }, [pauseForDiscard, registerCloseGuard, snapshot]);

  const mutateChoices = async (patch: Partial<PdfImportChoices>) => {
    if (!snapshot || mutationBusy) return;
    const choices = {
      ...snapshot.choices,
      ...patch,
      destination,
      expected_revision: snapshot.revision,
    };
    setMutationBusy(true);
    setError(null);
    try {
      publish(await api.updateChoices(projectId, snapshot.draft_id, choices));
    } catch (problem) {
      reportError(problem);
    } finally {
      setMutationBusy(false);
    }
  };

  const loadSamples = useCallback(async (category: PdfImportCategory, cursor?: string) => {
    if (!snapshot) return;
    try {
      const result = await api.samples(projectId, snapshot.draft_id, category, cursor);
      if (currentDraft.current !== snapshot.draft_id) return;
      setSampleFiles(result.items);
      setSampleCursor(result.next_cursor);
      const first = result.items[0] ?? null;
      setSelectedFileId(first?.occurrence_id ?? null);
      setTextSource('native');
      setPage(null);
      if (first && first.category !== 'other' && first.category !== 'unreadable') await loadPage(first);
    } catch (problem) {
      reportError(problem);
    }
  }, [api, loadPage, projectId, reportError, snapshot]);

  const chooseCategory = (category: PdfImportCategory) => {
    setSelectedCategory(category);
    setSampleFiles([]);
    setSelectedFileId(null);
    void loadSamples(category);
  };

  const selectSample = (file: PdfImportFileSummary) => {
    setSelectedFileId(file.occurrence_id);
    setTextSource('native');
    setPage(null);
    if (file.category !== 'other' && file.category !== 'unreadable') void loadPage(file);
  };

  const reconcileFiles = async (selected: File[]) => {
    if (!snapshot || reselecting) return;
    const pdfs = selected.filter((file) => file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf'));
    if (pdfs.length === 0) {
      reportError(new Error('Select the original PDF files or folder.'));
      return;
    }
    setReselecting(true);
    setError(null);
    const upload = new AbortController();
    uploadController.current?.abort();
    uploadController.current = upload;
    try {
      if (snapshot.paused) publish(await api.resume(projectId, snapshot.draft_id, { signal: upload.signal }));
      const pending: PdfImportUpload[] = [];
      for (let start = 0; start < pdfs.length; start += 256) {
        const batch = pdfs.slice(start, start + 256);
        const facts = batch.map((file) => ({
          logical_path: file.webkitRelativePath || file.name,
          filename: file.name,
          size: file.size,
        }));
        const matched = await api.reconcileReselection(
          projectId, snapshot.draft_id, facts, { signal: upload.signal },
        );
        matched.items.forEach((item, index) => {
          if (item.matched && item.upload_needed && item.occurrence_id) {
            pending.push({ fileId: item.occurrence_id, file: batch[index], logicalPath: item.logical_path });
          }
        });
      }
      for (let start = 0; start < pending.length; start += 64) {
        publish(await api.upload(
          projectId,
          snapshot.draft_id,
          pending.slice(start, start + 64),
          crypto.randomUUID(),
          { signal: upload.signal },
        ));
      }
      await refresh();
    } catch (problem) {
      if (!upload.signal.aborted) reportError(problem);
    } finally {
      if (uploadController.current === upload) uploadController.current = null;
      setReselecting(false);
    }
  };

  const startPreview = async (fileId: string, selectedPage = 1, confirmation?: string) => {
    if (!snapshot || previewStarting) return;
    setPreviewStarting(true);
    setError(null);
    try {
      if (!confirmation) {
        const estimate = await api.estimateOcrPreview(
          projectId, snapshot.draft_id, fileId, selectedPage, snapshot.choices.ocr_engine,
        );
        const token = estimate.estimate?.promise_set_hash;
        if (estimate.estimate?.requires_confirmation) {
          if (!token) throw new Error('The OCR estimate did not include an authorization token.');
          setPreviewConsent({ estimate, fileId, page: selectedPage });
          return;
        }
        if (estimate.cached) {
          const file = sampleFiles.find((item) => item.occurrence_id === fileId);
          if (file) await loadPage(file);
          setTextSource('ocr');
          return;
        }
      }
      await api.startOcrPreview(
        projectId, snapshot.draft_id, fileId, selectedPage,
        snapshot.choices.ocr_engine, confirmation,
      );
      setTextSource('ocr');
      await refresh();
    } catch (problem) {
      reportError(problem);
    } finally {
      setPreviewStarting(false);
    }
  };

  const compareDestination = async (next: PdfImportDestination) => {
    if (!snapshot) return;
    setDestination(next);
    setError(null);
    setMutationBusy(true);
    try {
      const updated = await api.updateChoices(projectId, snapshot.draft_id, {
        ...snapshot.choices,
        destination: next,
        expected_revision: snapshot.revision,
      });
      publish(updated);
      if (next.kind === 'existing') publish(await api.match(projectId, snapshot.draft_id));
    } catch (problem) {
      reportError(problem);
    } finally {
      setMutationBusy(false);
    }
  };

  const estimateBatchOcr = async (source = snapshot) => {
    if (!source || batchEstimateBusy) return;
    if (ocrPolicyCount(source, source.choices.ocr_policy) === 0) {
      setBatchOcrConsent(null);
      return;
    }
    setBatchEstimateBusy(true);
    setError(null);
    try {
      const estimate = await api.estimateBatchOcr(
        projectId,
        source.draft_id,
        source.choices.ocr_engine,
        source.choices.ocr_policy,
        source.revision,
      );
      setBatchOcrConsent(estimate.needs_confirmation ? estimate : null);
      await refresh();
    } catch (problem) {
      reportError(problem);
    } finally {
      setBatchEstimateBusy(false);
    }
  };

  const confirmDiscard = async () => {
    if (!snapshot || discarding) return;
    setDiscardPrompt(false);
    setDiscarding(true);
    uploadController.current?.abort();
    setError(null);
    try {
      publish(await api.discard(projectId, snapshot.draft_id));
      await refresh();
    } catch (problem) {
      reportError(problem);
    } finally {
      setDiscarding(false);
    }
  };

  const keepDraft = async () => {
    if (!snapshot) return;
    setDiscardPrompt(false);
    try {
      publish(await api.resume(projectId, snapshot.draft_id));
    } catch (problem) {
      reportError(problem);
    }
  };

  const commit = async () => {
    if (!snapshot || mutationBusy || !destinationIsReady(snapshot, destination)) return;
    const semanticSetup = snapshot.semantic_setup;
    if ((semanticEnabled ?? snapshot.choices.semantic_search) && semanticSetup?.needs_confirmation && !semanticConsent) return;
    setMutationBusy(true);
    setError(null);
    try {
      const accepted = await api.commit(projectId, snapshot.draft_id, {
        idempotency_key: commitKey.current,
        expected_revision: snapshot.revision,
      });
      committed.current = true;
      const sheetId = accepted.job?.sheet_id;
      if (sheetId) {
        importedSheet.current = sheetId;
        onImported(sheetId);
      }
      await refresh();
    } catch (problem) {
      if (!(problem instanceof PdfImportResponseError)) committed.current = true;
      reportError(problem);
      void refresh();
    } finally {
      setMutationBusy(false);
    }
  };

  if (!snapshot) {
    return <div className="pdf-batch-loading" role="status"><LoaderCircle className="spin" size={18} /> Preparing PDF import…</div>;
  }

  if (snapshot.state === 'cancelling' || snapshot.state === 'cancelled') {
    const total = snapshot.cleanup.bytes_total ?? snapshot.upload.bytes_done ?? 0;
    const done = snapshot.cleanup.bytes_done ?? (snapshot.state === 'cancelled' ? total : 0);
    return (
      <section className="pdf-batch-cancelling" data-testid="pdf-batch-cancelling">
        {snapshot.state === 'cancelling' && <LoaderCircle className="spin" size={22} aria-hidden />}
        <h2>Cancelling import</h2>
        <p>Removing copied files · {bytesLabel(done)} of {bytesLabel(total)}</p>
        <progress max={Math.max(1, total)} value={done} />
        <span>Nothing was added to your project. This closes by itself when done.</span>
        <button type="button" className="btn" onClick={onDiscarded}>Close</button>
      </section>
    );
  }

  const categories = visibleCategories(snapshot);
  const samples = sampleFiles;
  const checkRunning = snapshot.analysis.files_done < (snapshot.analysis.files_total ?? Infinity);
  const totalFiles = snapshot.upload.files_total ?? files.length;
  const semanticSearch = semanticEnabled ?? snapshot.choices.semantic_search;
  const semanticNeedsConsent = semanticSearch && snapshot.semantic_setup.needs_confirmation;
  const semanticAuthorized = !semanticNeedsConsent || (
    semanticConsent
    && snapshot.choices.semantic_setup_confirmation === snapshot.semantic_setup.confirmation_hash
  );
  const matching = destination.kind === 'existing' && snapshot.match.state === 'running';
  const nothingNew = destination.kind === 'existing'
    && snapshot.match.state === 'ready' && snapshot.match.new_count === 0;
  const canCommit = destinationIsReady(snapshot, destination)
    && !matching && semanticAuthorized
    && (ocrPolicyCount(snapshot, snapshot.choices.ocr_policy) === 0
      || (snapshot.batch_ocr?.state === 'ready'
        && (!snapshot.batch_ocr.needs_confirmation || Boolean(snapshot.choices.ocr_confirmation))))
    && snapshot.state === 'open' && !mutationBusy;

  return (
    <>
      <div className="pdf-batch-flow" data-stage={stage} data-testid="pdf-batch-flow">
        {stage === 'add' && (
          <AddFilesStep
            snapshot={snapshot}
            totalFiles={totalFiles}
            needsReselection={Boolean(resumeDraftId && snapshot.upload.files_done < snapshot.upload.files_total)}
            reselecting={reselecting}
            onReselect={(selected) => void reconcileFiles(selected)}
            onPause={() => void (snapshot.paused
              ? api.resume(projectId, snapshot.draft_id).then(publish).catch(reportError)
              : api.pause(projectId, snapshot.draft_id).then(publish).catch(reportError))}
            onCancel={pauseForDiscard}
            onContinue={() => {
              const category = CHECK_CATEGORY_ORDER.find((item) => (snapshot.category_counts[item] ?? 0) > 0);
              if (category) chooseCategory(category);
              setStage('check');
            }}
          />
        )}
        {stage === 'check' && (
          <CheckTextStep
            projectId={projectId}
            snapshot={snapshot}
            categories={categories}
            selectedCategory={selectedCategory}
            samples={samples}
            selectedFileId={selectedFileId}
            chosenFile={chosenFile}
            page={page}
            pageLoading={pageLoading}
            textSource={textSource}
            previewRunning={previewStarting || previewJob?.status === 'queued' || previewJob?.status === 'running'}
            checkRunning={checkRunning}
            onCategory={chooseCategory}
            onSample={selectSample}
            onResample={() => {
              void loadSamples(selectedCategory, sampleCursor ?? undefined);
            }}
            onTextSource={setTextSource}
            onPreview={() => chosenFile && void startPreview(chosenFile.occurrence_id)}
            onEngine={(engine) => void mutateChoices({ ocr_engine: engine })}
            onPolicy={(ocrPolicy) => {
              setBatchOcrConsent(null);
              void mutateChoices({ ocr_policy: ocrPolicy, ocr_confirmation: null });
            }}
            onBack={() => setStage('add')}
            onContinue={() => {
              setStage('destination');
              if (destination.kind === 'existing') void compareDestination(destination);
            }}
          />
        )}
        {stage === 'destination' && (
          <DestinationStep
            snapshot={snapshot}
            destination={destination}
            sheets={destinationSheets}
            busy={mutationBusy}
            onDestination={(next) => void compareDestination(next)}
            onBack={() => setStage('check')}
            onContinue={() => {
              setStage('search');
              void api.refreshSemanticSetup(projectId, snapshot.draft_id).then((refreshed) => {
                publish(refreshed);
                return estimateBatchOcr(refreshed);
              }).catch(reportError);
            }}
          />
        )}
        {stage === 'search' && (
          <SearchSetupStep
            snapshot={snapshot}
            semanticEnabled={semanticSearch}
            consent={semanticConsent
              && snapshot.choices.semantic_setup_confirmation === snapshot.semantic_setup.confirmation_hash}
            busy={mutationBusy}
            importLabel={nothingNew ? 'Nothing new to import' : matching ? 'Comparing…' : `Import ${count(
              destination.kind === 'existing' && snapshot.match.state === 'ready'
                ? snapshot.match.new_count : totalFiles,
              'PDF', 'PDFs',
            )}`}
            canImport={canCommit}
            batchEstimateBusy={batchEstimateBusy}
            onEstimateOcr={() => void estimateBatchOcr()}
            onSemantic={(enabled) => {
              setSemanticEnabled(enabled);
              setSemanticConsent(false);
              if (!enabled || !snapshot.semantic_setup.needs_confirmation) {
                void mutateChoices({ semantic_search: enabled, semantic_setup_confirmation: null });
              }
            }}
            onConsent={(consent) => {
              setSemanticConsent(consent);
              if (consent) void mutateChoices({
                semantic_search: true,
                semantic_setup_confirmation: snapshot.semantic_setup.confirmation_hash,
              });
            }}
            onBack={() => setStage('destination')}
            onImport={() => void commit()}
          />
        )}
        {error && <div className="pdf-batch-error" role="alert">{error}</div>}
      </div>
      {discardPrompt && (
        <div className="modal-backdrop" data-testid="pdf-batch-discard-dialog">
          <form className="modal-card" onSubmit={(event) => { event.preventDefault(); void confirmDiscard(); }}>
            <div className="modal-title"><AlertTriangle size={15} className="modal-warn-icon" /> Discard this import?</div>
            <p className="modal-body-text">Copied files and your choices will be removed. Nothing has been added to the project.</p>
            <div className="form-actions">
              <button type="button" className="btn" onClick={() => void keepDraft()}>Keep working</button>
              <button type="submit" className="btn btn-danger" disabled={discarding}>Discard import</button>
            </div>
          </form>
        </div>
      )}
      {previewConsent?.estimate.estimate && (
        <CostGateModal
          estimate={previewConsent.estimate.estimate}
          message={`Apply ${snapshot.choices.ocr_engine} OCR to this page?`}
          onCancel={() => setPreviewConsent(null)}
          onConfirm={() => {
            const pending = previewConsent;
            setPreviewConsent(null);
            void startPreview(pending.fileId, pending.page, pending.estimate.estimate?.promise_set_hash);
          }}
        />
      )}
      {batchOcrConsent?.estimate && (
        <CostGateModal
          estimate={batchOcrConsent.estimate}
          message={`Apply ${snapshot.choices.ocr_engine} OCR to ${count(batchOcrConsent.selected_count, 'file')} in this import?`}
          onCancel={() => setBatchOcrConsent(null)}
          onConfirm={() => {
            const token = batchOcrConsent.estimate?.promise_set_hash;
            setBatchOcrConsent(null);
            if (!token) {
              reportError(new Error('The batch OCR estimate did not include an authorization token.'));
              return;
            }
            void mutateChoices({ ocr_confirmation: token });
          }}
        />
      )}
    </>
  );
}
