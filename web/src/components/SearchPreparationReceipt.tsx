import { CircleAlert, LoaderCircle, Square } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  createSearchPreparation,
  decideSearchPreparation,
  getSearchPreparation,
  getSearchPreparationTextSamples,
  stopSearchPreparation,
  type SearchPreparationBatch,
  type SearchPreparationGroup,
  type SearchPreparationTextSample,
} from '../api/searchPreparation';
import { getModelPull, type ModelPullDto } from '../api/open';
import { formatNumberDisplay } from '../format';
import { usePoll } from '../hooks/usePoll';
import { ModelPullProgress } from './ModelPullProgress';
import { PdfTextPagePreview } from './importWorkspace/pdfPacket/PdfTextPagePreview';
import { resamplePages, samplePages } from './importWorkspace/pdfPacket/model';

const TERMINAL = new Set<SearchPreparationBatch['status']>(['complete', 'stopped', 'failed']);

function groupLabel(group: SearchPreparationGroup): { primary: string; qualifier: string } {
  const noun = group.count === 1 ? 'file' : 'files';
  if (group.kind === 'native_pdf') {
    return { primary: `${group.count} ${group.count === 1 ? 'PDF' : 'PDFs'}`, qualifier: 'with embedded text' };
  }
  if (group.kind === 'ocr') return { primary: `${group.count} ${noun}`, qualifier: 'to read with OCR' };
  return { primary: `${group.count} ${noun}`, qualifier: 'to transcribe' };
}

function groupStatus(group: SearchPreparationGroup): string {
  switch (group.status) {
    case 'waiting_for_download': return 'Waiting for download';
    case 'queued': return 'Queued';
    case 'running': return group.kind === 'transcribe' ? 'Transcribing' : 'Reading text';
    case 'complete': return 'Searchable';
    case 'not_prepared': return 'Not prepared';
    case 'stopped': return 'Stopped';
    case 'failed': return 'Needs attention';
    case 'needs_consent': return 'Needs your approval';
    default: return 'Checking';
  }
}

function modelPurposeLabel(purpose: SearchPreparationBatch['models'][number]['purpose']): string {
  if (purpose === 'read_text') return 'Read text in scans and images';
  if (purpose === 'transcribe') return 'Transcribe recordings and video';
  return 'Search by meaning';
}

function PullProgress({ pullId, onChange }: { pullId: number; onChange(): void }) {
  const [pull, setPull] = useState<ModelPullDto | null>(null);
  const [loadError, setLoadError] = useState(false);
  const loadGeneration = useRef(0);
  useEffect(() => () => {
    loadGeneration.current += 1;
  }, [pullId]);
  usePoll(async () => {
    const generation = loadGeneration.current;
    try {
      const next = await getModelPull(pullId);
      if (generation !== loadGeneration.current) return 'stop';
      setPull(next);
      setLoadError(false);
      return 'stop';
    } catch {
      if (generation === loadGeneration.current) setLoadError(true);
    }
  }, {
    active: pull === null,
    intervalMs: 1_000,
    immediate: true,
    mode: 'settle-relative',
  });
  return pull ? (
    <ModelPullProgress
      pull={pull}
      onDone={onChange}
      onFailed={onChange}
      readOnly
    />
  ) : (
    <div className="form-hint" role={loadError ? 'alert' : 'status'}>
      {loadError ? 'Could not refresh download progress. Retrying…' : 'Loading download progress…'}
    </div>
  );
}

function TextReview({
  projectId,
  batch,
  onBatchChange,
  onError,
}: {
  projectId: string;
  batch: SearchPreparationBatch;
  onBatchChange(batch: SearchPreparationBatch): void;
  onError(message: string): void;
}) {
  const [dismissed, setDismissed] = useState(false);
  const [sample, setSample] = useState<SearchPreparationTextSample | null>(null);
  const [pages, setPages] = useState<number[]>([]);
  const [loading, setLoading] = useState(true);
  const [forcing, setForcing] = useState(false);
  const loadGeneration = useRef(0);
  const loadController = useRef<AbortController | null>(null);

  useEffect(() => () => {
    loadGeneration.current += 1;
    loadController.current?.abort();
  }, [batch.id, projectId]);

  const load = async (rowId?: number, pageNumber?: number) => {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    const generation = ++loadGeneration.current;
    setLoading(true);
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
      if (next) {
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

  usePoll(load, {
    active: !dismissed && sample === null,
    intervalMs: 1_000,
    guardOverlap: true,
    maxAttempts: 1,
    immediate: true,
  });

  const selectPage = (page: number) => {
    if (!sample || sample.page_scoped) return;
    void load(sample.row_id, page);
  };
  const forceOcr = async () => {
    if (!sample || forcing) return;
    setForcing(true);
    try {
      const next = await createSearchPreparation(projectId, {
        sheet_id: batch.sheet_id,
        row_ids: [sample.row_id],
        force_ocr: true,
      });
      onBatchChange(next);
      setDismissed(true);
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setForcing(false);
    }
  };

  return (
    <>
      {!dismissed && (
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
                truncated={sample.truncated}
                loading={loading}
              />
              <p className="form-hint">
                OCR replaces the text Frisket uses for search. Your PDF stays unchanged.
              </p>
              <div className="search-preparation-actions">
                <button
                  type="button"
                  className="btn"
                  data-testid="search-preparation-force-ocr"
                  disabled={forcing}
                  onClick={() => void forceOcr()}
                >
                  {forcing ? 'Starting…' : 'No, replace with OCR'}
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  data-testid="search-preparation-keep-text"
                  onClick={() => setDismissed(true)}
                >
                  Yes, keep it
                </button>
              </div>
            </>
          ) : loading ? (
            <div role="status"><LoaderCircle className="spin" size={18} /> Reading PDF text…</div>
          ) : <p>No PDF text sample is available.</p>}
        </section>
      )}
    </>
  );
}

export function SearchPreparationReceipt({
  projectId,
  batchId,
  onBatchChange,
  onDone,
  onError,
  onViewJobs,
}: {
  projectId: string;
  batchId: string;
  onBatchChange?(batch: SearchPreparationBatch): void;
  onDone(): void;
  onError(message: string): void;
  onViewJobs?(): void;
}) {
  const [batch, setBatch] = useState<SearchPreparationBatch | null>(null);
  const [loadingError, setLoadingError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [semanticSelected, setSemanticSelected] = useState(true);
  const requestGeneration = useRef(0);
  const mutationGeneration = useRef<number | null>(null);

  const publish = (next: SearchPreparationBatch) => {
    setBatch(next);
    setLoadingError(null);
    onBatchChange?.(next);
  };
  const refresh = async () => {
    if (mutationGeneration.current !== null) return;
    const generation = ++requestGeneration.current;
    try {
      const next = await getSearchPreparation(projectId, batchId);
      if (generation === requestGeneration.current) publish(next);
    } catch (error) {
      if (generation === requestGeneration.current) {
        setLoadingError(error instanceof Error ? error.message : String(error));
      }
    }
  };

  useEffect(() => () => {
    requestGeneration.current += 1;
    mutationGeneration.current = null;
  }, [batchId, projectId]);

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
      const next = await decideSearchPreparation(projectId, batch.id, {
        plan_hash: batch.plan_hash,
        decision,
        selected_setup_refs: decision === 'prepare' ? selectedRefs : [],
      });
      if (generation === requestGeneration.current) publish(next);
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
  const stop = async () => {
    if (!batch || busy) return;
    const generation = ++requestGeneration.current;
    mutationGeneration.current = generation;
    setBusy(true);
    try {
      const next = await stopSearchPreparation(projectId, batch.id);
      if (generation === requestGeneration.current) publish(next);
    } catch (error) {
      if (generation === requestGeneration.current) {
        onError(error instanceof Error ? error.message : String(error));
      }
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
        <div role="status"><LoaderCircle className="spin" size={18} /> Checking search preparation…</div>
        {loadingError && <div className="import-field-error" role="alert">{loadingError}</div>}
      </section>
    );
  }

  const nativeGroup = batch.groups.find((group) => group.kind === 'native_pdf' && group.count > 0);
  const fullyPrepared = batch.status === 'complete'
    && batch.groups.length > 0
    && batch.groups.every((group) => group.status === 'complete');
  const active = !TERMINAL.has(batch.status);
  const missingModels = batch.models.filter((model) => !model.ready);
  return (
    <section className="search-preparation-receipt" data-testid="search-preparation-receipt">
      <header>
        <div>
          <h2>{fullyPrepared
            ? 'Ready to search'
            : batch.status === 'planning'
              ? 'Planning search preparation'
            : batch.status === 'complete' || batch.status === 'stopped'
              ? 'Files imported'
              : 'Make these files searchable'}</h2>
        </div>
      </header>

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
              <strong>{groupStatus(group)}</strong>
            </div>
          );
        })}
      </div>

      {active && batch.groups.length === 0 && (
        <div className="search-preparation-status" role="status">
          <LoaderCircle className="spin" size={16} /> Checking files and planning preparation…
        </div>
      )}

      {nativeGroup && (
        <TextReview
          projectId={projectId}
          batch={batch}
          onBatchChange={publish}
          onError={onError}
        />
      )}

      {batch.status === 'needs_consent' && (
        <section className="search-preparation-consent" data-testid="search-preparation-model-consent">
          <h3>{missingModels.length > 0 ? 'Download local models?' : 'Prepare files?'}</h3>
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
          {!batch.can_setup && (
            <div className="import-field-error" role="alert">
              Model setup is unavailable. See the model details above.
            </div>
          )}
        </section>
      )}

      {batch.models.some((model) => model.pull_id != null) && (
        <div className="search-preparation-pulls">
          {batch.models.map((model) => model.pull_id == null ? null : (
            <PullProgress key={model.pull_id} pullId={model.pull_id} onChange={() => void refresh()} />
          ))}
        </div>
      )}

      {batch.errors.length > 0 && (
        <div className="import-field-error" role="alert">
          <CircleAlert size={14} /> {batch.errors.join(' ')}
        </div>
      )}

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
            disabled={busy || !batch.can_setup}
            onClick={() => void decide('prepare')}
          >
            {busy ? 'Starting…' : hasSelectedDownloads ? 'Download and prepare' : 'Prepare files'}
          </button>
        </footer>
      ) : (
        <footer className="search-preparation-actions">
          {active && onViewJobs && batch.jobs.length > 0 && (
            <button
              type="button"
              className="btn"
              data-testid="search-preparation-view-jobs"
              onClick={onViewJobs}
            >
              View Jobs
            </button>
          )}
          {batch.can_stop && (
            <button
              type="button"
              className="btn"
              data-testid="search-preparation-stop"
              disabled={busy}
              onClick={() => void stop()}
            >
              <Square size={12} /> Stop remaining preparation
            </button>
          )}
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
