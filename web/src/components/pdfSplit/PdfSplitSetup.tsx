import { count } from '../importWorkspace/pdfBatch/model';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Row, SheetMeta } from '../../api/types';
import type { GridApiPort } from '../../api/ports';
import { createPdfPacketSplitFromSheet, getPdfPacketSheetScopeToken, deletePdfPacketSplit, getPdfPacketSplit, getPdfPacketPage, pdfPacketThumbnailUrl, estimatePdfPacketBatchOcr, estimatePdfPacketOcr, startPdfPacketOcr, type PdfPacketSheetScopeRequest, type PdfPacketSplitSnapshot } from '../../api/pdfPacketSplits';
import type { SelectedGridRows } from '../../workspace/workspaceState';
import { PdfSplitCheckText } from './PdfSplitCheckText';
import { Modal } from '../Modal';
import { StageStepper } from '../StageStepper';
import { SplitSetupFooter, SplitSetupHeader, SplitSetupPreview, SplitSetupSettings, type SplitSource } from './PdfSplitSetupSections';
import type { PdfImportOcrSelection } from '../../api/pdfImports';
import type { RunEstimate } from '../../api/open';
import { PdfPacketReview } from '../importWorkspace/pdfPacket/PdfPacketReview';
import { selectionScope } from './selectionScope';
import './PdfSplitSetup.css';

type Source = SplitSource & { prepared_complete: boolean };
type ScopeSnapshot = PdfPacketSplitSnapshot & { sources: Source[]; sheet_scope: { order_hash: string; excluded: Array<{ row_id: number; reason: string }> } | null };
function newerSnapshot(current: ScopeSnapshot | null, next: ScopeSnapshot): ScopeSnapshot | null {
  if (!current || current.split_id !== next.split_id || next.analysis_revision < current.analysis_revision) return current;
  return next;
}

export type PdfSplitApply = { snapshot: PdfPacketSplitSnapshot; organization: 'pages' | 'split_documents'; destination: { kind: 'new_sheet'; name: string } | { kind: 'replace'; sheet_id: number }; carriedColumnIds: number[]; idempotencyKey: string; ocrPolicy: 'missing' | 'missing_or_suspicious' | 'all' | 'none'; ocrSelection: PdfImportOcrSelection; ocrConfirmation: string | null; starts: Record<string, number[]> };

export function PdfSplitSetup({ projectId, sheet, fileColumnId, selection, view, getSheetRows, onClose, onApply }: {
  projectId: string; sheet: SheetMeta; fileColumnId?: string | null; selection: SelectedGridRows;
  view: Omit<PdfPacketSheetScopeRequest, 'sheet_id' | 'file_column_id' | 'organization' | 'scope'>;
  getSheetRows: GridApiPort['getSheetRows'];
  onClose(): void; onApply(input: PdfSplitApply): Promise<void>;
}) {
  const fileColumn = fileColumnId ? sheet.columns.find((column) => column.id === fileColumnId && column.type === 'file') : sheet.columns.find((column) => column.type === 'file');
  const admittedFileColumnId = fileColumn?.id ?? null;
  const hasSelection = selection.rowIds.length > 0 || selection.rowIndexes.length > 0;
  const hasFilter = Boolean(view.filter_);
  const scopeInputKey = useMemo(() => JSON.stringify({ selection, view }), [selection, view]);
  const idempotencyKey = useRef(crypto.randomUUID());
  const [scope, setScope] = useState<'selected' | 'filtered' | 'all'>(hasSelection ? 'selected' : hasFilter ? 'filtered' : 'all');
  const scopeOptions: Array<'selected' | 'filtered' | 'all'> = [...(hasSelection ? ['selected' as const] : []), ...(hasFilter ? ['filtered' as const] : []), 'all'];
  const [organization, setOrganization] = useState<'pages' | 'split_documents'>('pages');
  const [destination, setDestination] = useState<'new_sheet' | 'replace'>('new_sheet');
  const [name, setName] = useState(`${sheet.name} · pages`);
  const [carried, setCarried] = useState<string[]>([]);
  const [session, setSession] = useState<ScopeSnapshot | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [checkingText, setCheckingText] = useState(false);
  const [checkRequired, setCheckRequired] = useState(false);
  const [policy, setPolicy] = useState<'missing' | 'missing_or_suspicious' | 'all' | 'none'>('missing_or_suspicious');
  const [ocrSelection, setOcrSelection] = useState<PdfImportOcrSelection>({ kind: 'engine', engine: 'rapidocr' });
  const [ocrPolicyAccepted, setOcrPolicyAccepted] = useState(false);
  const [ocrConfirmation, setOcrConfirmation] = useState<string | null>(null);
  const [ocrQuote, setOcrQuote] = useState<{ estimate: RunEstimate; pages: number } | null>(null);
  const [previewRows, setPreviewRows] = useState<Row[]>([]);
  const [previewText, setPreviewText] = useState<Record<string, string>>({});
  const [previewKinds, setPreviewKinds] = useState<Record<string, 'native' | 'ocr' | null>>({});
  const [samplePage, setSamplePage] = useState<({ preview_text?: string | null; preview_truncated?: boolean; thumbnail_url?: string; ocr_text?: string | null; ocr_engine?: string | null } & { page: number }) | null>(null);
  const [sampleSourceKey, setSampleSourceKey] = useState<string | null>(null);
  const [sampleTextSource, setSampleTextSource] = useState<'native' | 'ocr'>('native');
  const [sampleConsent, setSampleConsent] = useState<RunEstimate | null>(null);
  const [sampleRescan, setSampleRescan] = useState(false);
  const [sampleJob, setSampleJob] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const actionLifetime = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    actionLifetime.current = controller;
    return () => { controller.abort(); if (actionLifetime.current === controller) actionLifetime.current = null; };
  }, []);
  const reviewClose = useRef<(() => void) | null>(null);
  const registerReviewClose = useCallback((handler: () => void) => {
    reviewClose.current = handler;
    return () => { if (reviewClose.current === handler) reviewClose.current = null; };
  }, []);
  const closeModal = () => {
    if (reviewing) reviewClose.current?.();
    else onClose();
  };
  const onReviewSnapshot = useCallback((next: PdfPacketSplitSnapshot) => setSession((current) => newerSnapshot(current, next as ScopeSnapshot)), []);
  useEffect(() => {
    if (!admittedFileColumnId) return undefined;
    const controller = new AbortController();
    let admitted: ScopeSnapshot | null = null;
    const frozen = JSON.parse(scopeInputKey) as { selection: SelectedGridRows; view: typeof view };
    const base: PdfPacketSheetScopeRequest = { ...frozen.view, sheet_id: Number(sheet.id), file_column_id: Number(admittedFileColumnId), organization, scope: frozen.view.filter_ ? 'filtered' : 'all' };
    void (async () => {
      await Promise.resolve();
      if (controller.signal.aborted) return;
      setSession(null); setReviewing(false); setCheckingText(false); setCheckRequired(false);
      setSampleSourceKey(null); setSampleTextSource('native'); setPolicy('missing_or_suspicious');
      setOcrConfirmation(null); setOcrPolicyAccepted(false); setOcrQuote(null); setPreviewText({}); setPreviewKinds({}); setPreviewRows([]); setError(null);
      idempotencyKey.current = crypto.randomUUID();
      try {
        if (scope === 'selected') {
          const chosen = selectionScope(frozen.selection);
          if ('selected_index_ranges' in chosen) {
            const token = await getPdfPacketSheetScopeToken(projectId, {
              sheet_id: base.sheet_id, scope: 'selected', filter_: base.filter_, sort: base.sort,
              parent_row_id: base.parent_row_id, scope_row_ids: base.scope_row_ids,
              ranked_row_ids: base.ranked_row_ids,
            }, { signal: controller.signal });
            admitted = await createPdfPacketSplitFromSheet(projectId, {
              ...base, scope, ...chosen, expected_order_hash: token.order_hash,
            }, { signal: controller.signal }) as ScopeSnapshot;
          } else {
            admitted = await createPdfPacketSplitFromSheet(projectId, { ...base, scope, ...chosen }, { signal: controller.signal }) as ScopeSnapshot;
          }
        } else {
          admitted = await createPdfPacketSplitFromSheet(projectId, { ...base, scope }, { signal: controller.signal }) as ScopeSnapshot;
        }
        if (!controller.signal.aborted) setSession(admitted);
        else if (admitted) void deletePdfPacketSplit(projectId, admitted.split_id).catch(() => undefined);
      } catch (e) { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : String(e)); }
    })();
    return () => { controller.abort(); if (admitted) void deletePdfPacketSplit(projectId, admitted.split_id).catch(() => undefined); };
  }, [projectId, sheet.id, admittedFileColumnId, organization, scope, scopeInputKey]);
  useEffect(() => {
    if (reviewing || !session || (session.status !== 'preparing' && (session.status !== 'ready' || !session.sources.some((source) => source.text_category == null)))) return undefined;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void getPdfPacketSplit(projectId, session.split_id, { signal: controller.signal })
        .then((next) => { if (!controller.signal.aborted) setSession((current) => newerSnapshot(current, next as ScopeSnapshot)); })
        .catch((e: unknown) => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : String(e)); });
    }, 1000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [projectId, session, reviewing]);
  const sources = session?.sources ?? [];
  const pageCount = sources.reduce((sum, source) => sum + source.page_end - source.page_start + 1, 0);
  const awaitingText = sources.some((source) => source.text_category == null);
  const needsText = sources.some((source) => source.text_category === 'missing' || source.text_category === 'suspicious');
  const stages = [
    { id: 'setup', label: 'Set up', testId: 'pdf-split-stage-setup' },
    ...((checkRequired || needsText || checkingText) ? [{ id: 'check', label: 'Check text', testId: 'pdf-split-stage-check' }] : []),
    ...(organization === 'split_documents' ? [{ id: 'find', label: 'Find documents', testId: 'pdf-split-stage-find' }] : []),
  ];
  const needsTextSources = sources.filter((source) => source.text_category === 'missing' || source.text_category === 'suspicious');
  const sample = sources.find((source) => source.key === sampleSourceKey) ?? needsTextSources[0];
  const splitId = session?.split_id;
  const revision = session?.analysis_revision;
  const status = session?.status;
  const sampleDisplayPage = sample?.display_start;
  const sourceProjectionKey = JSON.stringify(sources.map(({ key, page_start, page_end, display_start, source_row_id }) => ({ key, page_start, page_end, display_start, source_row_id })));
  const previewSources = useMemo(() => JSON.parse(sourceProjectionKey) as Array<Pick<Source, 'key' | 'page_start' | 'page_end' | 'display_start' | 'source_row_id'>>, [sourceProjectionKey]);
  useEffect(() => {
    if (!checkingText || !splitId || sampleDisplayPage == null) return undefined;
    const controller = new AbortController();
    void getPdfPacketPage(projectId, splitId, sampleDisplayPage, { signal: controller.signal })
      .then((page) => { if (!controller.signal.aborted) setSamplePage(page); })
      .catch((e: unknown) => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : String(e)); });
    return () => controller.abort();
  }, [checkingText, projectId, splitId, revision, sampleDisplayPage]);
  useEffect(() => {
    if (reviewing || !splitId || status !== 'ready') return undefined;
    const controller = new AbortController();
    const targets: Array<{ key: string; page: number }> = [];
    for (const source of previewSources) {
      const last = organization === 'pages' ? source.page_end : source.page_start;
      for (let local = source.page_start; local <= last && targets.length < 9; local += 1) {
        targets.push({ key: `${source.key}:${local}`, page: source.display_start + local - source.page_start });
      }
      if (targets.length === 9) break;
    }
    void Promise.all(targets.map(async (target) => {
      const page = await getPdfPacketPage(projectId, splitId, target.page, { signal: controller.signal });
      return { key: target.key, text: (page.preview_text ?? '').slice(0, 160), kind: page.preview_text_kind ?? null };
    })).then((rows) => {
      if (!controller.signal.aborted) {
        setPreviewText(Object.fromEntries(rows.map((row) => [row.key, row.text])));
        setPreviewKinds(Object.fromEntries(rows.map((row) => [row.key, row.kind])));
      }
    }).catch((e: unknown) => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : String(e)); });
    return () => controller.abort();
  }, [projectId, splitId, status, revision, organization, previewSources, reviewing]);
  useEffect(() => {
    if (reviewing || !splitId || !carried.length) return undefined;
    let active = true;
    const rowIds = [...new Set(previewSources.slice(0, 9).flatMap((source) => source.source_row_id == null ? [] : [source.source_row_id]))];
    if (!rowIds.length) return undefined;
    void getSheetRows(sheet.id, 0, rowIds.length, { rowIds, columnIds: carried, valueMode: 'bounded' })
      .then((result) => { if (active) setPreviewRows(result.rows); })
      .catch((e: unknown) => { if (active) setError(e instanceof Error ? e.message : String(e)); });
    return () => { active = false; };
  }, [getSheetRows, sheet.id, splitId, previewSources, carried, reviewing]);
  const apply = async (
    starts: Record<string, number[]>,
    approved?: { policy: typeof policy; confirmation: string | null },
  ) => {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      await onApply({
        snapshot: session,
        idempotencyKey: idempotencyKey.current,
        organization,
        destination: destination === 'new_sheet' ? { kind: 'new_sheet', name } : { kind: 'replace', sheet_id: Number(sheet.id) },
        carriedColumnIds: carried.map(Number),
        ocrPolicy: approved?.policy ?? (ocrPolicyAccepted ? policy : 'none'),
        ocrSelection,
        ocrConfirmation: approved ? approved.confirmation : ocrPolicyAccepted ? ocrConfirmation : null,
        starts,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      throw e;
    } finally {
      setBusy(false);
    }
  };
  const startSampleOcr = async (confirmation?: string, forceRescan = sampleRescan) => {
    if (!session || !sample) return;
    setBusy(true); setError(null); setSampleSourceKey(sample.key);
    const signal = actionLifetime.current?.signal;
    try {
      const result = await startPdfPacketOcr(projectId, session.split_id, { ocr_selection: ocrSelection, force_rescan: forceRescan, scope: 'sample', pages: [sample.display_start], ...(confirmation ? { confirmation } : {}) }, { signal });
      if (signal?.aborted) return;
      setSampleTextSource('ocr');
      if (result.job_id == null) {
        const [page, next] = await Promise.all([
          getPdfPacketPage(projectId, session.split_id, sample.display_start, { signal }),
          getPdfPacketSplit(projectId, session.split_id, { signal }),
        ]);
        if (signal?.aborted) return;
        setSamplePage(page);
        setSession((current) => newerSnapshot(current, next as ScopeSnapshot));
      } else setSampleJob(String(result.job_id));
    } catch (e) { if (!signal?.aborted) setError(e instanceof Error ? e.message : String(e)); }
    finally { if (!signal?.aborted) setBusy(false); }
  };
  const quoteSampleOcr = async (forceRescan = false) => {
    if (!session || !sample) return;
    setBusy(true); setError(null);
    const signal = actionLifetime.current?.signal;
    try {
      setSampleRescan(forceRescan);
      const quote = await estimatePdfPacketOcr(projectId, session.split_id, { ocr_selection: ocrSelection, force_rescan: forceRescan, scope: 'sample', pages: [sample.display_start] }, { signal });
      if (signal?.aborted) return;
      if (!forceRescan && !quote.estimate && quote.cached_pages.includes(sample.display_start)) {
        // Bind the cached result to this original without starting provider work.
        await startSampleOcr(undefined, false);
      } else if (!quote.estimate) {
        throw new Error('The OCR estimate reported no work and no cached page.');
      } else if (quote.estimate.requires_confirmation) {
        if (!quote.estimate.promise_set_hash) throw new Error('The OCR estimate did not include its authorization token.');
        setSampleConsent(quote.estimate);
      } else await startSampleOcr(undefined, forceRescan);
    } catch (e) { if (!signal?.aborted) setError(e instanceof Error ? e.message : String(e)); } finally { if (!signal?.aborted) setBusy(false); }
  };
  useEffect(() => {
    if (!sampleJob || !session || !sample) return undefined;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void getPdfPacketSplit(projectId, session.split_id, { signal: controller.signal }).then(async (next) => {
        if (controller.signal.aborted) return;
        const job = next.jobs.find((item) => item.job_id === sampleJob);
        if (job && ['done', 'error', 'cancelled'].includes(job.progress.status)) {
          if (job.progress.error) setError(job.progress.error);
          else if (job.progress.status === 'done') {
            const page = await getPdfPacketPage(projectId, session.split_id, sample.display_start, { signal: controller.signal });
            if (controller.signal.aborted) return;
            setSamplePage(page);
          }
          setSampleJob(null);
        }
        setSession((current) => newerSnapshot(current, next as ScopeSnapshot));
      }).catch((e: unknown) => { if (!controller.signal.aborted) { setSampleJob(null); setError(e instanceof Error ? e.message : String(e)); } });
    }, 1000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [projectId, sampleJob, session, sample]);
  const advanceFromCheck = async (confirmation: string | null) => {
    setOcrConfirmation(confirmation);
    setOcrPolicyAccepted(true);
    setCheckingText(false);
    if (organization === 'split_documents') {
      setReviewing(true);
    } else {
      await apply({}, { policy, confirmation }).catch(() => undefined);
    }
  };
  const quoteOcr = async () => {
    if (!session) return;
    if (policy === 'none') { await advanceFromCheck(null); return; }
    setBusy(true); setError(null);
    const signal = actionLifetime.current?.signal;
    try {
      const quote = await estimatePdfPacketBatchOcr(projectId, session.split_id, { expected_analysis_revision: session.analysis_revision, ocr_policy: policy, ocr_selection: ocrSelection }, { signal });
      if (signal?.aborted) return;
      if (quote.pages > 0 && !quote.estimate) throw new Error('An OCR estimate is required before continuing.');
      if (quote.estimate?.requires_confirmation) {
        if (!quote.estimate.promise_set_hash) throw new Error('The OCR estimate did not include its authorization token.');
        setOcrQuote({ estimate: quote.estimate, pages: quote.pages });
      } else await advanceFromCheck(null);
    } catch (e) { if (!signal?.aborted) setError(e instanceof Error ? e.message : String(e)); } finally { if (!signal?.aborted) setBusy(false); }
  };
  if (!fileColumn) return <Modal title="Split PDFs" hideHeader className="pdf-split-modal" onClose={closeModal}><div className="pdf-split-setup">
    <SplitSetupHeader title="Split PDFs" subtitle={sheet.name} onClose={onClose} />
    <p role="alert">This sheet has no file column.</p>
  </div></Modal>;
  if (checkingText && session && sample) return <Modal title="Check text" hideHeader className="pdf-split-modal" onClose={closeModal}><PdfSplitCheckText
    projectId={projectId} stages={stages} sources={needsTextSources} totalSourceCount={sources.length} sample={sample} samplePage={samplePage?.page === sample.display_start ? samplePage : null}
    thumbnailUrl={pdfPacketThumbnailUrl(projectId, session.split_id, sample.display_start)}
    textSource={sampleTextSource} selection={ocrSelection} policy={policy} busy={busy} sampleJob={sampleJob}
    sampleConsent={sampleConsent} quote={ocrQuote} error={error}
    primaryLabel={organization === 'split_documents' ? 'Find documents →' : destination === 'replace'
      ? `Replace ${count(sources.length, 'row')} with ${pageCount}` : `Create ${count(pageCount, 'row')}`}
    onSource={(key) => { setSampleSourceKey(key); setSampleTextSource('native'); }}
    onTextSource={setSampleTextSource}
    onPreview={() => ocrSelection.kind === 'engine' && samplePage?.ocr_text != null && samplePage.ocr_engine === ocrSelection.engine
      ? setSampleTextSource('ocr') : void quoteSampleOcr()}
    onRescan={() => void quoteSampleOcr(true)}
    onSelection={(value) => {
      setOcrSelection((old) => JSON.stringify(old) === JSON.stringify(value) ? old : value);
      if (JSON.stringify(ocrSelection) !== JSON.stringify(value)) { setOcrConfirmation(null); setOcrPolicyAccepted(false); setSampleTextSource('native'); }
    }}
    onPolicy={(value) => { setPolicy(value); setOcrConfirmation(null); setOcrPolicyAccepted(false); }}
    onCancelSampleConsent={() => setSampleConsent(null)}
    onConfirmSampleConsent={() => { const hash = sampleConsent?.promise_set_hash; setSampleConsent(null); if (hash) void startSampleOcr(hash); }}
    onCancelQuote={() => setOcrQuote(null)}
    onConfirmQuote={() => { const hash = ocrQuote?.estimate.promise_set_hash; setOcrQuote(null); if (hash) void advanceFromCheck(hash); }}
    onBack={() => setCheckingText(false)} onClose={onClose} onContinue={() => void quoteOcr()} /></Modal>;
  if (reviewing && session) return <Modal title="Split PDFs" hideHeader className="pdf-split-modal" onClose={closeModal}><div className="pdf-split-setup">
    <PdfPacketReview projectId={projectId} snapshot={session} onClose={onClose} registerCloseRequest={registerReviewClose}
      headerContent={stages.length > 1 ? <StageStepper label="Split stages" stages={stages} stage="find" /> : null} onSnapshotChange={onReviewSnapshot}
      discardText="Text read so far stays on the original rows."
      primaryLabel={(documents) => destination === 'replace' ? `Replace ${count(sources.length, 'row')} with ${documents}` : `Create ${count(documents, 'row')}`}
      onBack={() => setReviewing(false)} onReviewed={apply} />
  </div></Modal>;
  const primaryLabel = needsText ? 'Check text →' : organization === 'split_documents' ? 'Find documents →'
    : destination === 'replace' ? `Replace ${count(sources.length, 'row')} with ${pageCount}` : `Create ${count(pageCount, 'row')}`;
  return <Modal title="Split PDFs" hideHeader className="pdf-split-modal" onClose={closeModal}><div className="pdf-split-setup">
    <SplitSetupHeader title="Split PDFs" subtitle={sheet.name} onClose={onClose} />
    {stages.length > 1 && <StageStepper label="Split stages" stages={stages} stage="setup" />}
    <div className="pdf-split-setup-body">
      <SplitSetupSettings sheet={sheet} fileColumnId={fileColumn.id} scopeOptions={scopeOptions} scope={scope}
        onScope={setScope} organization={organization} onOrganization={(value) => {
          setOrganization(value); setName(`${sheet.name} · ${value === 'pages' ? 'pages' : 'documents'}`);
        }} destination={destination} onDestination={setDestination} name={name} onName={setName}
        carried={carried} onCarried={setCarried} sourceCount={sources.length} pageCount={pageCount} />
      <SplitSetupPreview sheet={sheet} organization={organization} sources={sources} carried={carried}
        rows={previewRows} text={previewText} textKinds={previewKinds} excluded={session?.sheet_scope?.excluded ?? []} />
    </div>
    {error && <p role="alert">{error}</p>}
    {session?.status === 'error' && <p role="alert">{session.prepare.progress.error ?? 'Could not prepare these PDFs.'}</p>}
    {awaitingText && session?.status !== 'error' && <p role="status">Checking text…</p>}
    <SplitSetupFooter onBack={onClose} note={(session?.sheet_scope?.excluded.length ?? 0) === 1 ? '1 row stays as it is' : (session?.sheet_scope?.excluded.length ?? 0) > 1 ? `${session?.sheet_scope?.excluded.length} rows stay as they are` : undefined}
      primary={primaryLabel} disabled={!session || session.status !== 'ready' || awaitingText || busy || sources.length === 0 || (destination === 'new_sheet' && !name.trim())}
      onPrimary={() => {
        if (needsText) { setCheckRequired(true); setCheckingText(true); }
        else if (organization === 'split_documents') setReviewing(true);
        else void apply({}).catch(() => undefined);
      }} />
  </div></Modal>;
}
