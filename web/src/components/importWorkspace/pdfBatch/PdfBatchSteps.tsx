import { AlertTriangle, CheckCircle2, ExternalLink, FileText, LoaderCircle, Pause, Play, RefreshCw } from 'lucide-react';
import type { ReactNode } from 'react';

import type { PdfImportCategory, PdfImportChoices, PdfImportDestination, PdfImportFileSummary, PdfImportOcrSelection, PdfImportPage, PdfImportSnapshot } from '../../../api/pdfImports';
import type { SelectorChoice } from '../../../api/selectorChoices';
import { SelectorField } from '../../../engine-selector/SelectorField';
import { PanelSelect } from '../../PanelSelect';
import { SegmentedToggle } from '../../PanelPrimitives';
import { PdfTextPagePreview } from '../pdfPacket/PdfTextPagePreview';
import { routePath } from '../../../routes';
import type { PdfBatchDestinationSheet } from './PdfBatchImportFlow';
import { PDF_CATEGORY_LABEL, PDF_CATEGORY_ORDER, PDF_CATEGORY_TONE, bytesLabel, canOpenCheckText, count, destinationIsReady, ocrPolicyCount } from './model';

const CHECK_CATEGORY_ORDER: readonly PdfImportCategory[] = ['suspicious', 'missing', 'text', 'unreadable'];

function ocrSelectionOf(choice: SelectorChoice): PdfImportOcrSelection | null {
  const selected = choice.authored_selection;
  if (selected.kind === 'engine') return selected;
  if (selected.kind === 'engine_model') return selected;
  return null;
}

function sameOcrSelection(left: PdfImportOcrSelection, right: PdfImportOcrSelection): boolean {
  return left.kind === right.kind && left.engine === right.engine
    && (left.kind !== 'engine_model' || right.kind !== 'engine_model' || left.model === right.model);
}

function pageUsesSelection(page: PdfImportPage, selection: PdfImportOcrSelection): boolean {
  const params = page.ocr_action_params;
  if (!params || params.engine !== selection.engine) return false;
  if (selection.kind === 'engine') return !Object.hasOwn(params, 'model');
  return Object.hasOwn(params, 'model') && (params.model ?? null) === selection.model;
}

export function AddFilesStep({
  snapshot, totalFiles, needsReselection, reselecting, retryingPreflight, onReselect, onPause,
  onRetryPreflight, onCancel, onContinue,
}: {
  snapshot: PdfImportSnapshot;
  totalFiles: number;
  needsReselection: boolean;
  reselecting: boolean;
  retryingPreflight: boolean;
  onReselect(files: File[]): void;
  onPause(): void;
  onRetryPreflight(): void;
  onCancel(): void;
  onContinue(): void;
}) {
  const reading = snapshot.upload.files_total == null;
  const uploadTotal = snapshot.upload.files_total ?? totalFiles;
  const uploadBytesTotal = snapshot.upload.bytes_total ?? 0;
  const checkedTotal = snapshot.analysis.files_total ?? snapshot.upload.files_done;
  const uploadComplete = snapshot.upload.files_done >= uploadTotal;
  return <section className="pdf-batch-step pdf-batch-add" aria-label="Add files">
    <div className="pdf-batch-step-body">
      <header><h2>Add files</h2><p>Files are copied to a holding area. Nothing is added to the project until Import.</p></header>
      {snapshot.preflight.state === 'insufficient' ? <div className="pdf-batch-preflight-error" role="alert">
        <AlertTriangle size={18} />
        <div><strong>Not enough disk space</strong><span>This import needs {bytesLabel(uploadBytesTotal)}. {bytesLabel(snapshot.preflight.free_bytes ?? 0)} is free.</span></div>
        <button type="button" className="btn" disabled={retryingPreflight} onClick={onRetryPreflight}>{retryingPreflight ? 'Checking…' : 'Check again'}</button>
        <button type="button" className="btn" onClick={onCancel}>Choose different files</button>
      </div> : <><div className="pdf-batch-progress-grid">
        <ProgressCard
          title={reading ? 'Reading folder' : uploadComplete ? 'Copying complete' : 'Copying files'}
          done={snapshot.upload.files_done}
          total={reading ? null : uploadTotal}
          detail={reading
            ? `${count(snapshot.upload.files_done, 'file')} found so far`
            : `${bytesLabel(snapshot.upload.bytes_done ?? 0)} of ${bytesLabel(uploadBytesTotal)} copied`}
          action={uploadComplete ? <span className="pdf-batch-complete"><CheckCircle2 size={14} /> Complete</span> : <button type="button" className="btn btn-quiet" onClick={onPause}>
            {snapshot.paused ? <Play size={14} /> : <Pause size={14} />}{snapshot.paused ? 'Resume' : 'Pause'}
          </button>}
        />
        <ProgressCard
          title="Checking text layers"
          done={snapshot.analysis.files_done}
          total={checkedTotal || null}
          detail={`${count(snapshot.analysis.files_done, 'PDF')} checked · ${count(snapshot.upload.files_done, 'PDF')} copied`}
        />
      </div>
      <p className="pdf-batch-space">{snapshot.preflight.state === 'waiting'
        ? <LoaderCircle className="spin" size={14} aria-hidden />
        : <CheckCircle2 size={14} aria-hidden />}
        {snapshot.preflight.state === 'waiting'
          ? `Checking space for ${bytesLabel(uploadBytesTotal)}.`
          : snapshot.preflight.state === 'ready'
            ? `Enough space · needs ${bytesLabel(uploadBytesTotal)} · ${bytesLabel(snapshot.preflight.free_bytes ?? 0)} free`
            : `More disk space is needed · needs ${bytesLabel(uploadBytesTotal)} · ${bytesLabel(snapshot.preflight.free_bytes ?? 0)} free`}
      </p></>}
      {needsReselection && <div className="pdf-batch-reselect" role="status">
        <div><strong>Some files still need to be copied</strong><span>Select the same files or folder again. Only pending PDFs are uploaded.</span></div>
        <label className="btn">
          {reselecting ? 'Matching files…' : 'Select files again'}
          <input
            type="file"
            accept=".pdf,application/pdf"
            multiple
            hidden
            disabled={reselecting}
            onChange={(event) => {
              onReselect(Array.from(event.currentTarget.files ?? []));
              event.currentTarget.value = '';
            }}
          />
        </label>
        <label className="btn">
          {reselecting ? 'Matching folder…' : 'Select folder again'}
          <input
            type="file"
            accept=".pdf,application/pdf"
            multiple
            hidden
            disabled={reselecting}
            ref={(node) => node?.setAttribute('webkitdirectory', '')}
            onChange={(event) => {
              onReselect(Array.from(event.currentTarget.files ?? []));
              event.currentTarget.value = '';
            }}
          />
        </label>
      </div>}
      <CategoryTable snapshot={snapshot} showUnchecked />
    </div>
    <StepFooter>
      <button type="button" className="btn" onClick={onCancel}>Cancel</button>
      <span>{canOpenCheckText(snapshot)
        ? 'You can review samples while the remaining files are copied.'
        : 'Check text opens once each type has a few files.'}</span>
      <button type="button" className="btn btn-primary" disabled={!canOpenCheckText(snapshot)} onClick={onContinue}>Check text →</button>
    </StepFooter>
  </section>;
}

function ProgressCard({ title, done, total, detail, action }: {
  title: string;
  done: number;
  total: number | null;
  detail: string;
  action?: ReactNode;
}) {
  return <article className="pdf-batch-progress-card">
    <header><strong>{title}</strong>{action}</header>
    <div><b>{total == null ? `${done.toLocaleString()} found so far` : `${done.toLocaleString()} of ${total.toLocaleString()}`}</b><span>{detail}</span></div>
    <progress max={Math.max(1, total ?? 1)} value={total == null ? 1 : done} data-indeterminate={total == null || undefined} />
  </article>;
}

function CategoryTable({ snapshot, showUnchecked = false }: { snapshot: PdfImportSnapshot; showUnchecked?: boolean }) {
  return <div className="pdf-batch-category-table" role="table" aria-label="Found so far">
    <div role="row" className="pdf-batch-category-heading"><strong role="columnheader">Found so far</strong><strong role="columnheader">Files</strong></div>
    {PDF_CATEGORY_ORDER.filter((category) => category !== 'image' && category !== 'other' && (showUnchecked || category !== 'unchecked')).map((category) => {
      const value = snapshot.category_counts[category] ?? 0;
      if (!showUnchecked && value === 0) return null;
      return <div role="row" key={category}>
        <span role="cell"><i style={{ background: PDF_CATEGORY_TONE[category] }} />{PDF_CATEGORY_LABEL[category]}</span>
        <b role="cell">{value.toLocaleString()}</b>
      </div>;
    })}
  </div>;
}

export function CheckTextStep({
  projectId, snapshot, categories, selectedCategory, samples, selectedFileId, chosenFile,
  samplesLoading, page, pageLoading, textSource, previewRunning, checkRunning, onCategory, onSample,
  busy, onResample, onTextSource, onPreview, onOcrChoice, onOcrSelection, onPolicy, onBack, onContinue,
}: {
  projectId: string;
  snapshot: PdfImportSnapshot;
  categories: PdfImportCategory[];
  selectedCategory: PdfImportCategory;
  samples: PdfImportFileSummary[];
  samplesLoading: boolean;
  selectedFileId: string | null;
  chosenFile: PdfImportFileSummary | null;
  page: PdfImportPage | null;
  pageLoading: boolean;
  textSource: 'native' | 'ocr';
  previewRunning: boolean;
  checkRunning: boolean;
  busy: boolean;
  onCategory(category: PdfImportCategory): void;
  onSample(file: PdfImportFileSummary): void;
  onResample(): void;
  onTextSource(source: 'native' | 'ocr'): void;
  onPreview(): void;
  onOcrChoice(choice: SelectorChoice | null): void;
  onOcrSelection(selection: PdfImportOcrSelection): void;
  onPolicy(policy: PdfImportChoices['ocr_policy']): void;
  onBack(): void;
  onContinue(): void;
}) {
  const countFor = snapshot.category_counts[selectedCategory] ?? 0;
  const nonViewer = selectedCategory === 'other' || selectedCategory === 'unreadable';
  const currentOcr = snapshot.choices.ocr_selection;
  const ocrReady = Boolean(page && page.ocr_text !== null && pageUsesSelection(page, currentOcr));
  const shownText = textSource === 'ocr' ? page?.ocr_text ?? '' : page?.native_text ?? '';
  const unreadableCount = snapshot.category_counts.unreadable ?? 0;
  return <section className="pdf-batch-step pdf-batch-check" aria-label="Check text">
    <div className="pdf-batch-check-body">
      <aside className="pdf-batch-check-sidebar">
        <h2>Text from your files</h2>
        <p>{count(snapshot.analysis.files_done, 'PDF')} checked so far. Review representative files before choosing a batch OCR policy.</p>
        {checkRunning && <div className="pdf-batch-analysis-progress" role="status"><LoaderCircle className="spin" size={15} />
          <span>Checking text layers {snapshot.analysis.files_done.toLocaleString()} of {(snapshot.analysis.files_total ?? snapshot.upload.files_done).toLocaleString()}</span>
          <progress max={Math.max(1, snapshot.analysis.files_total ?? snapshot.upload.files_done)} value={snapshot.analysis.files_done} />
        </div>}
        <div className="pdf-batch-type-list">
          {CHECK_CATEGORY_ORDER.filter((category) => categories.includes(category)).map((category) => (
            <button type="button" key={category} className={selectedCategory === category ? 'active' : ''} onClick={() => onCategory(category)}>
              <i style={{ background: PDF_CATEGORY_TONE[category] }} />
              <span>{PDF_CATEGORY_LABEL[category]}</span>
              <b>{(snapshot.category_counts[category] ?? 0).toLocaleString()}{checkRunning ? ' ↑' : ''}</b>
            </button>
          ))}
        </div>
        {!nonViewer && <>
          <div className="pdf-batch-sample-heading"><strong>{countFor > 6 ? `Sample files · 6 of ${countFor.toLocaleString()}` : `Files · ${countFor.toLocaleString()}`}</strong>
            {countFor > 6 && <button type="button" className="icon-btn" aria-label="Show different sample files" onClick={onResample}><RefreshCw size={15} /></button>}
          </div>
          <div className="pdf-batch-samples">
            {samples.map((file) => <button type="button" key={file.occurrence_id} className={selectedFileId === file.occurrence_id ? 'active' : ''} onClick={() => onSample(file)} title={file.logical_path}>
              {file.thumbnail_url ? <img src={file.thumbnail_url} alt="" loading="lazy" /> : <span className="pdf-batch-sample-placeholder"><FileText size={20} /></span>}
              <small>{file.filename}</small>
            </button>)}
            {checkRunning && Array.from({ length: Math.max(0, 6 - samples.length) }, (_, index) => <span className="pdf-batch-empty-sample" key={index}><i /><small>More samples appear as files are checked.</small></span>)}
          </div>
        </>}
      </aside>
      <div className="pdf-batch-check-main">
        <section className="pdf-batch-preview">
          {nonViewer
            ? <FileIssueList files={samples} loading={samplesLoading} />
            : samplesLoading && !chosenFile
            ? <div className="pdf-batch-empty-preview" role="status"><LoaderCircle className="spin" size={16} /> Loading sample…</div>
            : chosenFile ? <>
            <header><div><strong>{chosenFile.filename}</strong><span>{chosenFile.page_count == null ? 'Reading pages…' : count(chosenFile.page_count, 'page')}</span></div>
            <SegmentedToggle ariaLabel="Text preview" className="segmented-toolbar" fullWidth={false} value={textSource} onValueChange={(value) => {
              if (value === 'ocr' && !ocrReady) onPreview();
              else onTextSource(value as 'native' | 'ocr');
            }} options={[{ value: 'native', label: 'Text layer' }, { value: 'ocr', label: 'OCR preview' }]} />
            </header>
            {selectedCategory === 'suspicious' && textSource === 'native' && <div className="pdf-batch-garbled"><AlertTriangle size={14} /> This text may be garbled. Try OCR preview to compare.</div>}
            <PdfTextPagePreview
              page={1}
              thumbnailUrl={page?.thumbnail_url ?? chosenFile.thumbnail_url}
              thumbnailAlt={`Page 1 of ${chosenFile.filename}`}
              heading={textSource === 'ocr' ? 'Extracted text' : 'Text layer in this PDF'}
              text={shownText}
              truncated={textSource === 'native' && Boolean(page?.native_text_truncated)}
              loading={pageLoading || (textSource === 'ocr' && previewRunning)}
              loadingCopy={textSource === 'ocr' ? 'Applying OCR to this page…' : 'Loading preview…'}
              emptyCopy={textSource === 'ocr'
                ? 'Choose OCR preview to read this page.'
                : selectedCategory === 'missing' ? 'This file has no text layer. Try OCR preview.' : 'No text was found on this page.'}
            />
          </> : <div className="pdf-batch-empty-preview">Select a sample file.</div>}
        </section>
        <div className="pdf-batch-ocr-controls">
          <div className="pdf-batch-ocr-controls-head"><strong>Apply OCR to</strong><div className="pdf-batch-engine">
            <span>OCR engine</span>
            <SelectorField
              projectId={projectId}
              label="OCR engine"
              query={{ schema_version: 'frisket.selector_choices_query.v1', subject: { kind: 'action', action_id: 'media.ocr', field: 'engine', params: { engine: currentOcr.engine, ...(currentOcr.kind === 'engine_model' ? { model: currentOcr.model } : {}) } } }}
              recentNamespace="pdf-batch-ocr"
              testId="pdf-batch-ocr-engine"
              disabled={busy}
              onSelect={(choice) => { const selection = ocrSelectionOf(choice); if (selection) onOcrSelection(selection); }}
              onCurrentChoiceChange={(choice) => {
                onOcrChoice(choice);
                const selection = choice ? ocrSelectionOf(choice) : null;
                if (selection && !sameOcrSelection(selection, currentOcr)) onOcrSelection(selection);
              }}
              allowChoice={(choice) => ocrSelectionOf(choice) !== null}
            />
          </div></div>
          <OcrPolicy snapshot={snapshot} checking={checkRunning} busy={busy} onPolicy={onPolicy} />
        </div>
      </div>
    </div>
    <StepFooter><button type="button" className="btn" disabled={busy} onClick={onBack}>Back</button><span>{checkRunning ? 'You can continue now. Files checked later follow your choice.' : `OCR will apply to ${count(ocrPolicyCount(snapshot, snapshot.choices.ocr_policy), 'file')}${unreadableCount > 0 ? ` · ${unreadableCount.toLocaleString()} couldn't be read` : ''}.`}</span><button type="button" className="btn btn-primary" disabled={busy} onClick={onContinue}>{busy ? 'Saving…' : 'Continue →'}</button></StepFooter>
  </section>;
}

function OcrPolicy({ snapshot, checking, busy, onPolicy }: { snapshot: PdfImportSnapshot; checking: boolean; busy: boolean; onPolicy(policy: PdfImportChoices['ocr_policy']): void }) {
  const options: Array<{ value: PdfImportChoices['ocr_policy']; label: string; detail: string }> = [
    { value: 'missing_or_suspicious', label: 'Missing or suspicious text', detail: 'Scans and PDFs whose text may be garbled' },
    { value: 'all', label: 'All PDFs', detail: 'Read every PDF with OCR' },
    { value: 'none', label: 'None', detail: 'text layers only' },
  ];
  return <fieldset className="pdf-batch-ocr-policy" aria-label="Apply OCR to">
    {options.map((option) => <label key={option.value} className={snapshot.choices.ocr_policy === option.value ? 'active' : ''}>
      <input type="radio" name="pdf-batch-ocr-policy" value={option.value} disabled={busy} checked={snapshot.choices.ocr_policy === option.value} onChange={() => onPolicy(option.value)} />
      <span><strong>{option.label}</strong><small>{option.detail}</small></span>
      <b>{count(ocrPolicyCount(snapshot, option.value), 'file')}{checking ? ' so far' : ''}</b>
    </label>)}
    {snapshot.choices.ocr_policy === 'none' && <p><AlertTriangle size={13} /> Files without a text layer won't be searchable until you run OCR on them.</p>}
  </fieldset>;
}

function FileIssueList({ files, loading }: { files: PdfImportFileSummary[]; loading: boolean }) {
  return <div className="pdf-batch-issue-list">
    <h3>Couldn't read these PDFs</h3>
    {loading
      ? <p role="status"><LoaderCircle className="spin" size={15} /> Loading file details…</p>
      : files.length > 0
        ? files.map((file) => <div key={file.occurrence_id}>
          <strong>{file.filename}</strong>
          <span>{file.error || "This file couldn't be opened."} It will be imported without text.</span>
        </div>)
        : <p>No file details are available. These PDFs will be imported without text.</p>}
  </div>;
}

export function DestinationStep({ projectId, snapshot, destination, sheets, busy, onDestination, onBack, onContinue }: {
  projectId: string;
  snapshot: PdfImportSnapshot;
  destination: PdfImportDestination;
  sheets: readonly PdfBatchDestinationSheet[];
  busy: boolean;
  onDestination(destination: PdfImportDestination): void;
  onBack(): void;
  onContinue(): void;
}) {
  const total = snapshot.upload.files_total ?? 0;
  const matching = destination.kind === 'existing' && (busy || snapshot.match.state === 'running');
  const matched = destination.kind === 'existing' ? snapshot.match.matched_count : 0;
  const importing = destination.kind === 'existing' && snapshot.match.state === 'ready' ? snapshot.match.new_count : total;
  const sheetName = destination.kind === 'existing'
    ? sheets.find((sheet) => sheet.id === destination.sheet_id)?.name ?? 'this sheet'
    : destination.sheet_name.trim() || 'the new sheet';
  const reviewCounts = destination.kind === 'existing' && snapshot.match.state === 'ready'
    ? snapshot.match.new_category_counts ?? snapshot.category_counts
    : snapshot.category_counts;
  return <section className="pdf-batch-step pdf-batch-destination" aria-label="Review destination">
    <div className="pdf-batch-step-body">
      <header><h2>Review destination</h2><p>Choose where these PDFs belong, then review exactly what will be added.</p></header>
      <fieldset className="pdf-batch-destination-options"><legend>Import into</legend>
        <div className={`pdf-batch-destination-card${destination.kind === 'new' ? ' active' : ''}`}>
          <label><input type="radio" name="destination" disabled={busy} checked={destination.kind === 'new'} onChange={() => onDestination({ kind: 'new', sheet_name: destination.kind === 'new' ? destination.sheet_name : 'Imported PDFs' })} /><span><strong>New sheet</strong><small>Create a sheet for this batch</small></span></label>
          {destination.kind === 'new' && <label className="pdf-batch-sheet-field">Sheet name<input className="form-input" disabled={busy} value={destination.sheet_name} onChange={(event) => onDestination({ kind: 'new', sheet_name: event.currentTarget.value })} /></label>}
        </div>
        <div className={`pdf-batch-destination-card${destination.kind === 'existing' ? ' active' : ''}`}>
          <label><input type="radio" name="destination" disabled={busy} checked={destination.kind === 'existing'} onChange={() => { const sheet = sheets[0]; if (sheet) onDestination({ kind: 'existing', sheet_id: sheet.id }); }} /><span><strong>Existing sheet</strong><small>Skip matching file names and sizes</small></span></label>
          {destination.kind === 'existing' && <label className="pdf-batch-sheet-field">Existing sheet<PanelSelect ariaLabel="Existing sheet" topLayer disabled={busy} value={destination.sheet_id} onValueChange={(value) => onDestination({ kind: 'existing', sheet_id: Number(value) })}>{sheets.map((sheet) => <option key={sheet.id} value={sheet.id}>{sheet.name}</option>)}</PanelSelect></label>}
        </div>
      </fieldset>
      {matching && <div className="pdf-batch-matching" role="status"><LoaderCircle className="spin" size={16} /> <span>Comparing with {sheets.find((sheet) => destination.kind === 'existing' && sheet.id === destination.sheet_id)?.name ?? 'sheet'} · {snapshot.match.checked.toLocaleString()} of {snapshot.match.total.toLocaleString()} checked</span><progress max={Math.max(1, snapshot.match.total)} value={snapshot.match.checked} /></div>}
      <p className="pdf-batch-destination-summary"><strong>{count(importing, destination.kind === 'existing' ? 'new PDF' : 'PDF')}</strong>{destination.kind === 'existing' && matched > 0 && <span>{count(matched, 'PDF')} already in {sheetName} and left as is</span>}</p>
      <div className="pdf-batch-destination-table" role="table" aria-label="PDF import review">
        <div role="row" className="pdf-batch-destination-heading"><strong role="columnheader">Type</strong><strong role="columnheader">Files</strong><strong role="columnheader">Text</strong></div>
        {destination.kind === 'existing' && matched > 0 && <div role="row" className="pdf-batch-existing-row">
          <span role="cell"><strong>Already in {sheetName}</strong><small>Same file name and size</small></span>
          <b role="cell">{matched.toLocaleString()}</b>
          <span role="cell"><i className="pdf-batch-text-badge neutral">Not imported</i><a className="link-btn" href={routePath({ kind: 'project', projectId, sheetId: String(destination.sheet_id) })} target="_blank" rel="noopener noreferrer" title="Open sheet in a new tab">View sheet <ExternalLink size={11} /></a></span>
        </div>}
        {PDF_CATEGORY_ORDER.filter((category) => category !== 'unchecked' && category !== 'image' && category !== 'other' && (reviewCounts[category] ?? 0) > 0).map((category) => <div role="row" key={category}>
          <span role="cell">{PDF_CATEGORY_LABEL[category]}</span>
          <b role="cell">{(reviewCounts[category] ?? 0).toLocaleString()}</b>
          <TextSourceBadge category={category} policy={snapshot.choices.ocr_policy} />
        </div>)}
      </div>
    </div>
    <StepFooter><button type="button" className="btn" disabled={busy} onClick={onBack}>Back</button><span>{matching
      ? 'Comparing file names and sizes…'
      : destination.kind === 'new' ? `Creates “${sheetName}”.` : `New rows are added at the end of ${sheetName}.`}</span><button type="button" className="btn btn-primary" disabled={busy || !destinationIsReady(snapshot, destination) || matching} onClick={onContinue}>{matching ? 'Comparing…' : busy ? 'Saving…' : 'Search setup →'}</button></StepFooter>
  </section>;
}

function TextSourceBadge({ category, policy }: { category: PdfImportCategory; policy: PdfImportChoices['ocr_policy'] }) {
  const useOcr = policy === 'all' || (policy === 'missing_or_suspicious'
    && (category === 'suspicious' || category === 'missing' || category === 'image'));
  if (useOcr) return <span role="cell"><i className="pdf-batch-text-badge ocr">OCR</i></span>;
  if (category === 'text' || category === 'suspicious') return <span role="cell"><i className="pdf-batch-text-badge native">Text layer</i></span>;
  if (category === 'missing' || category === 'image') return <span role="cell"><i className="pdf-batch-text-badge neutral">No text</i></span>;
  return <span role="cell">—</span>;
}

export function SearchSetupStep({ snapshot, semanticEnabled, consent, busy, importLabel, canImport, batchEstimateBusy, batchEstimateWaiting, batchEstimateError, ocrAvailabilityChecking, ocrAvailable, ocrProblem, onEstimateOcr, onSemantic, onConsent, onBack, onReviewOcr, onImport }: {
  snapshot: PdfImportSnapshot;
  semanticEnabled: boolean;
  consent: boolean;
  busy: boolean;
  importLabel: string;
  canImport: boolean;
  batchEstimateBusy: boolean;
  batchEstimateWaiting: boolean;
  batchEstimateError: string | null;
  ocrAvailabilityChecking: boolean;
  ocrAvailable: boolean;
  ocrProblem: string | null;
  onEstimateOcr(): void;
  onSemantic(enabled: boolean): void;
  onConsent(value: boolean): void;
  onBack(): void;
  onReviewOcr(): void;
  onImport(): void;
}) {
  const setup = snapshot.semantic_setup;
  const semanticWaitingForConsent = semanticEnabled && setup.needs_confirmation && !consent;
  const ocrDisplayName = snapshot.choices.ocr_selection.engine === 'rapidocr'
    ? 'RapidOCR'
    : snapshot.choices.ocr_selection.engine;
  return <section className="pdf-batch-step pdf-batch-search" aria-label="Search setup">
    <div className="pdf-batch-step-body">
      <header><h2>Search setup</h2><p>Keyword search is included. You can also prepare these documents for search by meaning.</p></header>
      <label className="pdf-batch-semantic-choice"><input type="checkbox" disabled={busy} checked={semanticEnabled} onChange={(event) => onSemantic(event.currentTarget.checked)} /><span><strong>Search by meaning</strong><small>Find related ideas even when the exact words differ.</small></span></label>
      {semanticEnabled && <div className="pdf-batch-model-setup" data-state={setup.ready ? 'ready' : setup.available ? 'setup' : 'unavailable'}>
        <div><strong>{setup.display_name}</strong><span>{setup.ready ? 'Ready on this device' : setup.runtime_ready ? 'Required for search by meaning' : 'Model and runtime setup required'}</span></div>
        <b>{setup.download_bytes == null ? 'Size not measured' : `About ${bytesLabel(setup.download_bytes)}`}</b>
        {setup.needs_confirmation && <label><input type="checkbox" disabled={busy} checked={consent} onChange={(event) => onConsent(event.currentTarget.checked)} /> Download this model when the import starts</label>}
        {!setup.available && <p role="alert">Model setup is unavailable.</p>}
      </div>}
      {ocrPolicyCount(snapshot, snapshot.choices.ocr_policy) > 0 && <div className="pdf-batch-ocr-review" data-state={!ocrAvailable ? 'setup' : snapshot.batch_ocr?.state ?? 'idle'}>
        <div><strong>OCR</strong><span>{count(ocrPolicyCount(snapshot, snapshot.choices.ocr_policy), 'file')} · {ocrDisplayName}</span></div>
        {ocrAvailabilityChecking
          ? <span className="pdf-batch-inline-progress"><LoaderCircle className="spin" size={13} /> Checking availability…</span>
          : !ocrAvailable
          ? <span title={ocrProblem ?? undefined}>Setup required · <button type="button" className="link-btn" onClick={onReviewOcr}>Back to OCR engine</button></span>
          : snapshot.choices.ocr_selection.engine === 'rapidocr'
          ? <b>Ready</b>
          : batchEstimateWaiting
            ? <span>Waiting for files…</span>
          : snapshot.batch_ocr?.state === 'ready'
          ? <b>Estimate ready</b>
            : snapshot.batch_ocr?.state === 'error' || batchEstimateError
              ? <button type="button" className="btn" title={batchEstimateError ?? snapshot.batch_ocr?.error ?? undefined} disabled={batchEstimateBusy} onClick={onEstimateOcr}>{batchEstimateBusy ? 'Checking…' : 'Try again'}</button>
              : <span className="pdf-batch-inline-progress"><LoaderCircle className="spin" size={13} /> Checking…</span>}
      </div>}
      {snapshot.paused && <p className="pdf-batch-paused-import" role="status">Copying is paused. Go back to Add files to resume before importing.</p>}
    </div>
    <StepFooter><button type="button" className="btn" disabled={busy} onClick={onBack}>Back</button><span>{snapshot.paused
      ? 'Copying is paused. Go back to Add files to resume.'
      : semanticWaitingForConsent
      ? `Allow the ${setup.download_bytes == null ? 'model' : bytesLabel(setup.download_bytes)} download, or turn off search by meaning.`
      : semanticEnabled ? 'Keyword search and search by meaning will be prepared.' : 'Keyword search will be prepared.'}</span><button type="button" className="btn btn-primary" disabled={!canImport} onClick={onImport}>{busy ? 'Importing…' : importLabel}</button></StepFooter>
  </section>;
}

function StepFooter({ children }: { children: ReactNode }) {
  return <footer className="pdf-batch-footer">{children}</footer>;
}

