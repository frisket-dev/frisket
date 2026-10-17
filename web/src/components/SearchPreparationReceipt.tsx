import { CircleAlert, LoaderCircle } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  replanSearchPreparation,
  decideSearchPreparation,
  stopSearchPreparation,
  getSearchPreparation,
  getSearchPreparationTextSamples,
  type SearchPreparationBatch,
  type SearchPreparationGroup,
  type SearchPreparationTextSample,
} from '../api/searchPreparation';
import { formatNumberDisplay } from '../format';
import { usePoll, type PollResult } from '../hooks/usePoll';
import { PdfTextPagePreview } from './importWorkspace/pdfPacket/PdfTextPagePreview';
import { resamplePages, samplePages } from './importWorkspace/pdfPacket/model';

const TERMINAL = new Set<SearchPreparationBatch['status']>(['complete', 'stopped', 'failed']);

function groupLabel(group: SearchPreparationGroup): { primary: string; qualifier: string } {
  const noun = group.count === 1 ? 'file' : 'files';
  if (group.kind === 'native_pdf') {
    return { primary: `${group.count} ${group.count === 1 ? 'PDF' : 'PDFs'}`, qualifier: 'with embedded text to check' };
  }
  if (group.kind === 'ocr') return { primary: `${group.count} ${noun}`, qualifier: 'requiring OCR' };
  return { primary: `${group.count} ${noun}`, qualifier: 'to transcribe' };
}

function modelPurposeLabel(purpose: SearchPreparationBatch['models'][number]['purpose']): string {
  if (purpose === 'read_text') return 'Read text in scans and images';
  if (purpose === 'transcribe') return 'Transcribe recordings and video';
  return 'Search by meaning';
}

function TextReview({
  projectId,
  batch,
  busy,
  onReplan,
  onError,
}: {
  projectId: string;
  batch: SearchPreparationBatch;
  busy: boolean;
  onReplan(forceOcr: boolean): void;
  onError(message: string): void;
}) {
  const [sample, setSample] = useState<SearchPreparationTextSample | null>(null);
  const [requestedStatus, setRequestedStatus] = useState<SearchPreparationBatch['status'] | null>(null);
  const [pages, setPages] = useState<number[]>([]);
  const [loading, setLoading] = useState(true);
  const loadGeneration = useRef(0);
  const loadController = useRef<AbortController | null>(null);

  useEffect(() => () => {
    loadGeneration.current += 1;
    loadController.current?.abort();
  }, [batch.id, projectId]);

  const load = async (rowId?: number, pageNumber?: number, background = false) => {
    setRequestedStatus(batch.status);
    if (background && loadController.current !== null) return;
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    const generation = ++loadGeneration.current;
    if (!background) setLoading(true);
    try {
      const result = await getSearchPreparationTextSamples(
        projectId,
        batch.id,
        { rowId, pageNumber },
        { signal: controller.signal },
      );
      if (controller.signal.aborted || generation !== loadGeneration.current) return;
      const next = result.samples.find((item) => (
        (rowId == null || item.row_id === rowId)
        && (pageNumber == null || item.page_number === pageNumber)
      )) ?? result.samples[0] ?? null;
      setSample(next);
      if (next && (!sample || sample.row_id !== next.row_id || sample.page_count !== next.page_count)) {
        setPages(next.page_scoped
          ? [next.page_number]
          : samplePages(next.page_count ?? next.page_number));
      }
    } catch (error) {
      if (!controller.signal.aborted && generation === loadGeneration.current) {
        onError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      if (generation === loadGeneration.current) {
        loadController.current = null;
        setLoading(false);
      }
    }
  };

  usePoll(() => load(sample?.row_id, sample?.page_number, sample !== null), {
    active: requestedStatus !== batch.status,
    intervalMs: 1_000,
    guardOverlap: true,
    maxAttempts: 1,
    immediate: true,
  });

  const selectPage = (page: number) => {
    if (!sample || sample.page_scoped) return;
    void load(sample.row_id, page);
  };

  return (
    <section className="search-preparation-text-preview" data-testid="search-preparation-text-preview">
      <header>
        <div>
          <strong>Does this text look right?</strong>
          <span>{sample?.filename ?? 'Imported PDF'}</span>
        </div>
      </header>
      {sample ? (
        <>
          <div className="search-preparation-sample-pages">
            {sample.page_scoped ? (
              <span>Page {sample.page_number} of {sample.page_count}</span>
            ) : pages.map((page) => (
                <button
                  type="button"
                  key={page}
                  className={page === sample.page_number ? 'active' : ''}
                  aria-pressed={page === sample.page_number}
                  onClick={() => selectPage(page)}
                >
                  Page {page}
                </button>
              ))}
            {!sample.page_scoped && (sample.page_count ?? 1) > 6 && (
              <button
                type="button"
                className="icon-btn"
                aria-label="Refresh sample pages"
                onClick={() => setPages((current) => resamplePages(sample.page_count ?? 1, current))}
              >
                ↻
              </button>
            )}
          </div>
          <PdfTextPagePreview
            page={sample.page_number}
            thumbnailUrl={sample.preview_url}
            text={sample.text}
            heading="Extracted text"
            emptyCopy="No embedded text is available on this sampled page."
            truncated={sample.truncated}
            loading={loading}
          />
        </>
      ) : loading ? (
        <div role="status"><LoaderCircle className="spin" size={18} /> Reading PDF text…</div>
      ) : <p>No PDF text sample is available.</p>}
      <div className="search-preparation-actions">
        <button type="button" className={batch.force_ocr ? 'btn' : 'btn btn-primary'} data-testid="search-preparation-keep-text"
          aria-pressed={!batch.force_ocr} disabled={busy}
          onClick={() => onReplan(false)}>Keep embedded text</button>
        <button type="button" className={batch.force_ocr ? 'btn btn-primary' : 'btn'} data-testid="search-preparation-force-ocr"
          aria-pressed={batch.force_ocr} disabled={busy}
          onClick={() => onReplan(true)}>Re-scan PDFs with OCR</button>
      </div>
      <p className="form-hint">
        This choice applies to all PDFs in this batch. Keep embedded text where available,
        or re-scan every PDF with OCR.
      </p>
    </section>
  );
}

export function SearchPreparationReceipt({
  projectId,
  batchId,
  onBatchChange,
  onDone,
  onError,
  importSummary,
}: {
  projectId: string;
  batchId: string;
  onBatchChange?(batch: SearchPreparationBatch): void;
  onDone(): void;
  onError(message: string): void;
  importSummary?: ReactNode;
}) {
  const [batch, setBatch] = useState<SearchPreparationBatch | null>(null);
  const [loadingError, setLoadingError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [semanticSelected, setSemanticSelected] = useState(true);
  const requestGeneration = useRef(0);
  const mutationGeneration = useRef<number | null>(null);
  const currentBatchId = useRef(batchId);
  const handedOff = useRef(false);
  const finish = () => {
    if (handedOff.current) return;
    handedOff.current = true;
    onDone();
  };

  const publish = (next: SearchPreparationBatch) => {
    currentBatchId.current = next.id;
    onBatchChange?.(next);
    if (next.status === 'preparing' && (next.jobs.some((job) => (
      job.status === 'queued' || job.status === 'running'
    )) || next.models.some((model) => model.pull_id != null))) {
      finish();
      return;
    }
    setBatch(next);
    setLoadingError(null);
  };
  const refresh = async (): Promise<PollResult> => {
    if (handedOff.current) return 'stop';
    if (mutationGeneration.current !== null) return;
    const generation = ++requestGeneration.current;
    try {
      const next = await getSearchPreparation(projectId, currentBatchId.current);
      if (generation === requestGeneration.current) publish(next);
      if (handedOff.current) return 'stop';
    } catch (error) {
      if (generation === requestGeneration.current) {
        setLoadingError(error instanceof Error ? error.message : String(error));
      }
    }
  };

  useEffect(() => () => {
    requestGeneration.current += 1;
    mutationGeneration.current = null;
  }, [projectId]);

  usePoll(refresh, {
    active: batch === null || !TERMINAL.has(batch.status),
    intervalMs: 1_500,
    guardOverlap: true,
    immediate: true,
  });

  const requiredRefs = useMemo(() => (
    batch?.models
      .filter((model) => model.required && !model.ready && model.can_setup)
      .map((model) => model.setup_ref) ?? []
  ), [batch]);
  const optionalModels = batch?.models.filter((model) => (
    !model.required && !model.ready && model.can_setup
  )) ?? [];
  const selectedRefs = [
    ...requiredRefs,
    ...(semanticSelected ? optionalModels.map((model) => model.setup_ref) : []),
  ];
  const hasSelectedDownloads = selectedRefs.length > 0;

  const decide = async (decision: 'prepare' | 'not_now') => {
    if (!batch || busy) return;
    const generation = ++requestGeneration.current;
    mutationGeneration.current = generation;
    let refreshAfterFailure = false;
    setBusy(true);
    try {
      const next = decision === 'not_now'
        ? await stopSearchPreparation(projectId, batch.id)
        : await decideSearchPreparation(projectId, batch.id, {
          plan_hash: batch.plan_hash,
          decision: 'prepare',
          selected_setup_refs: selectedRefs,
        });
      if (generation === requestGeneration.current) {
        publish(next);
        finish();
      }
    } catch (error) {
      if (generation === requestGeneration.current) {
        onError(error instanceof Error ? error.message : String(error));
        refreshAfterFailure = true;
      }
    } finally {
      if (mutationGeneration.current === generation) {
        mutationGeneration.current = null;
        setBusy(false);
      }
    }
    if (refreshAfterFailure && generation === requestGeneration.current) void refresh();
  };
  const replan = async (forceOcr: boolean) => {
    if (!batch || busy || batch.force_ocr === forceOcr) return;
    const generation = ++requestGeneration.current;
    mutationGeneration.current = generation;
    setBusy(true);
    try {
      const next = await replanSearchPreparation(projectId, batch.id, {
        plan_hash: batch.plan_hash, force_ocr: forceOcr,
      });
      if (generation === requestGeneration.current) publish(next);
    } catch (error) {
      if (generation === requestGeneration.current) onError(error instanceof Error ? error.message : String(error));
    } finally {
      if (mutationGeneration.current === generation) {
        mutationGeneration.current = null;
        setBusy(false);
      }
    }
  };

  if (!batch) {
    return (
      <section className="search-preparation-receipt" data-testid="search-preparation-receipt">
        <div className="search-preparation-receipt-body">
          {importSummary}
          <div role="status"><LoaderCircle className="spin" size={18} /> Checking search preparation…</div>
          {loadingError && <div className="import-field-error" role="alert">{loadingError}</div>}
        </div>
        <footer className="search-preparation-actions">
          <button type="button" className="btn" onClick={onDone}>Close</button>
        </footer>
      </section>
    );
  }

  const pdfGroup = batch.groups.find((group) => (
    (group.kind === 'native_pdf' || group.kind === 'ocr') && group.count > 0
  ));
  const ocrGroup = batch.groups.find((group) => group.kind === 'ocr' && group.count > 0);
  const fullyPrepared = batch.status === 'complete'
    && batch.groups.length > 0
    && batch.groups.every((group) => group.status === 'complete');
  const active = !TERMINAL.has(batch.status);
  const missingModels = batch.models.filter((model) => !model.ready);
  const unavailableModels = missingModels.filter((model) => model.required && !model.can_setup);
  const allRequiredModelsReady = batch.models.every((model) => !model.required || model.ready);
  const canPrepare = batch.can_setup || allRequiredModelsReady;
  const warnings = batch.warnings ?? [];
  return (
    <section className="search-preparation-receipt" data-testid="search-preparation-receipt">
      <div className="search-preparation-receipt-body">
        {importSummary}
        <header>
          <div>
            <h2>{fullyPrepared && warnings.length > 0
              ? 'Text prepared with warnings'
              : fullyPrepared
              ? 'Text ready for keyword search'
              : batch.status === 'planning'
                ? 'Planning search preparation'
              : batch.status === 'complete' || batch.status === 'stopped'
                ? 'Files imported'
                : 'Make these files searchable'}</h2>
          </div>
        </header>

        {warnings.length > 0 && (
          <aside className="form-hint" role="note" aria-label="Preparation warnings" data-testid="search-preparation-warnings">
            <strong><CircleAlert size={14} className="run-warning-icon" aria-hidden /> Some results need attention</strong>
            <ul>{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
          </aside>
        )}

        <div className="search-preparation-groups">
          {batch.groups.map((group) => {
            const label = groupLabel(group);
            return (
              <div
                key={group.kind}
                className="search-preparation-group"
                data-testid={`search-preparation-group-${group.kind}`}
                data-status={group.status}
              >
                <span><strong>{label.primary}</strong> <small>{label.qualifier}</small></span>
              </div>
            );
          })}
        </div>

        {active && batch.groups.length === 0 && (
          <div className="search-preparation-status" role="status">
            <LoaderCircle className="spin" size={16} /> Checking files and planning preparation…
          </div>
        )}

        {batch.status !== 'needs_consent' && unavailableModels.length > 0 && (
          <div className="import-field-error" role="alert">
            {unavailableModels.map((model) => (
              <p key={model.setup_ref}>
                <strong>{model.display_name} is unavailable on this device.</strong>{' '}
                {model.remediation || 'Install its local runtime, then prepare these files again.'}
              </p>
            ))}
          </div>
        )}

        {pdfGroup && batch.status === 'needs_consent' && (
          <TextReview
            key={batch.id}
            projectId={projectId}
            batch={batch}
            busy={busy}
            onReplan={(forceOcr) => void replan(forceOcr)}
            onError={onError}
          />
        )}

        {batch.status === 'needs_consent' && (
          <section className="search-preparation-consent" data-testid="search-preparation-model-consent">
            <h3>{missingModels.length > 0
              ? 'Set up text extraction'
              : ocrGroup ? 'Extract text with OCR?' : 'Prepare file text?'}</h3>
            <p className="form-hint">Preparing this batch will run {batch.groups.map((group) => (
              group.kind === 'ocr' ? `${group.count} ${group.count === 1 ? 'file' : 'files'} with OCR`
                : group.kind === 'transcribe' ? `${group.count} ${group.count === 1 ? 'transcription' : 'transcriptions'}`
                : `embedded-text extraction for ${group.count} ${group.count === 1 ? 'PDF' : 'PDFs'}`
            )).join(', ')}.</p>
            {missingModels.length > 0 && (
              <div className="search-preparation-model-list">
                {missingModels.map((model) => (
                  <label
                    className="search-preparation-model"
                    key={model.setup_ref}
                    data-testid={model.required
                      ? `search-preparation-model-${model.purpose}`
                      : undefined}
                  >
                    <input
                      type="checkbox"
                      data-testid={model.required ? undefined : 'search-preparation-semantic-model'}
                      checked={model.can_setup && (model.required || semanticSelected)}
                      disabled={model.required || !model.can_setup}
                      onChange={(event) => setSemanticSelected(event.currentTarget.checked)}
                    />
                    <span>
                      <strong>{model.display_name}</strong>
                      <small className="search-preparation-model-purpose">{modelPurposeLabel(model.purpose)}</small>
                      {!model.can_setup && model.remediation && (
                        <small className="search-preparation-model-remediation">{model.remediation}</small>
                      )}
                    </span>
                    <b>{model.download_bytes == null
                      ? 'Size not measured'
                      : `About ${formatNumberDisplay(model.download_bytes, 'filesize')}`}</b>
                  </label>
                ))}
              </div>
            )}
            {!canPrepare && (
              <div className="import-field-error" role="alert">
                Model setup is unavailable. See the model details above.
              </div>
            )}
          </section>
        )}

        {batch.errors.length > 0 && (
          <div className="import-field-error" role="alert">
            <CircleAlert size={14} /> {batch.errors.join(' ')}
          </div>
        )}
      </div>
      {batch.status === 'needs_consent' ? (
        <footer className="search-preparation-actions">
          <button
            type="button"
            className="btn"
            data-testid="search-preparation-decision-not-now"
            disabled={busy}
            onClick={() => void decide('not_now')}
          >
            Not now
          </button>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="search-preparation-decision-prepare"
            disabled={busy || !canPrepare}
            onClick={() => void decide('prepare')}
          >
            {busy
              ? 'Please wait…'
              : hasSelectedDownloads
                ? 'Download and prepare'
                : 'Prepare files'}
          </button>
        </footer>
      ) : (
        <footer className="search-preparation-actions">
          <button
            type="button"
            className="btn btn-primary"
            data-testid="search-preparation-done"
            onClick={onDone}
          >
            {active ? 'Close' : 'Done'}
          </button>
        </footer>
      )}
    </section>
  );
}
