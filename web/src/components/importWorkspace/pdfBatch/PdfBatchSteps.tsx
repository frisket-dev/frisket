import { AlertTriangle, CheckCircle2, FileText, LoaderCircle, Pause, Play, RefreshCw } from 'lucide-react';
import type { ReactNode } from 'react';

import type { PdfImportCategory, PdfImportChoices, PdfImportDestination, PdfImportFileSummary, PdfImportPage, PdfImportSnapshot } from '../../../api/pdfImports';
import type { SelectorChoice } from '../../../api/selectorChoices';
import { SelectorField } from '../../../engine-selector/SelectorField';
import { PanelSelect } from '../../PanelSelect';
import { SegmentedToggle } from '../../PanelPrimitives';
import { PdfTextPagePreview } from '../pdfPacket/PdfTextPagePreview';
import type { PdfBatchDestinationSheet } from './PdfBatchImportFlow';
import { PDF_CATEGORY_LABEL, PDF_CATEGORY_ORDER, PDF_CATEGORY_TONE, bytesLabel, canOpenCheckText, count, destinationIsReady, ocrPolicyCount } from './model';

const CHECK_CATEGORY_ORDER: readonly PdfImportCategory[] = ['suspicious', 'missing', 'text', 'image', 'other', 'unreadable'];

function engineOf(choice: SelectorChoice): string | null {
  const selected = choice.authored_selection;
  // The PDF import contract currently persists an engine id only. Keep model-specific
  // choices out of this picker until the draft contract can preserve the model too.
  return selected.kind === 'engine' ? selected.engine : null;
}

export function AddFilesStep({
  snapshot, totalFiles, needsReselection, reselecting, onReselect, onPause, onCancel, onContinue,
}: {
  snapshot: PdfImportSnapshot;
  totalFiles: number;
  needsReselection: boolean;
  reselecting: boolean;
  onReselect(files: File[]): void;
  onPause(): void;
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
      <div className="pdf-batch-progress-grid">
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
      </p>
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
    {PDF_CATEGORY_ORDER.filter((category) => showUnchecked || category !== 'unchecked').map((category) => {
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
  page, pageLoading, textSource, previewRunning, checkRunning, onCategory, onSample,
  onResample, onTextSource, onPreview, onEngine, onPolicy, onBack, onContinue,
}: {
  projectId: string;
  snapshot: PdfImportSnapshot;
  categories: PdfImportCategory[];
  selectedCategory: PdfImportCategory;
  samples: PdfImportFileSummary[];
  selectedFileId: string | null;
  chosenFile: PdfImportFileSummary | null;
  page: PdfImportPage | null;
  pageLoading: boolean;
  textSource: 'native' | 'ocr';
  previewRunning: boolean;
  checkRunning: boolean;
  onCategory(category: PdfImportCategory): void;
  onSample(file: PdfImportFileSummary): void;
  onResample(): void;
  onTextSource(source: 'native' | 'ocr'): void;
  onPreview(): void;
  onEngine(engine: string): void;
  onPolicy(policy: PdfImportChoices['ocr_policy']): void;
  onBack(): void;
  onContinue(): void;
}) {
  const countFor = snapshot.category_counts[selectedCategory] ?? 0;
  const nonViewer = selectedCategory === 'other' || selectedCategory === 'unreadable';
  const ocrReady = Boolean(page?.ocr_text && page.ocr_engine === snapshot.choices.ocr_engine);
  const shownText = textSource === 'ocr' ? page?.ocr_text ?? '' : page?.native_text ?? '';
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
          {nonViewer ? <FileIssueList files={samples} /> : chosenFile ? <>
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
              heading={textSource === 'ocr' ? `Text read with ${snapshot.choices.ocr_engine}` : 'Text layer in this PDF'}
              text={shownText}
              loading={pageLoading || (textSource === 'ocr' && previewRunning)}
              loadingCopy={textSource === 'ocr' ? 'Applying OCR to this page…' : 'Loading preview…'}
              emptyCopy={textSource === 'ocr'
                ? 'Choose OCR preview to read this page.'
                : selectedCategory === 'missing' ? 'This file has no text layer.' : 'No text was found on this page.'}
            />
          </> : <div className="pdf-batch-empty-preview">Select a sample file.</div>}
        </section>
        <div className="pdf-batch-ocr-controls">
          <div className="pdf-batch-ocr-controls-head"><strong>Apply OCR to</strong><div className="pdf-batch-engine">
            <span>OCR engine</span>
            <SelectorField
              projectId={projectId}
              label="OCR engine"
              query={{ schema_version: 'frisket.selector_choices_query.v1', subject: { kind: 'action', action_id: 'media.ocr', field: 'engine', params: { engine: snapshot.choices.ocr_engine } } }}
              recentNamespace="pdf-batch-ocr"
              testId="pdf-batch-ocr-engine"
              onSelect={(choice) => { const engine = engineOf(choice); if (engine) onEngine(engine); }}
              onCurrentChoiceChange={(choice) => { const engine = choice ? engineOf(choice) : null; if (engine && engine !== snapshot.choices.ocr_engine) onEngine(engine); }}
              allowChoice={(choice) => engineOf(choice) !== null}
            />
          </div></div>
          <OcrPolicy snapshot={snapshot} checking={checkRunning} onPolicy={onPolicy} />
        </div>
      </div>
    </div>
    <StepFooter><button type="button" className="btn" onClick={onBack}>Back</button><span>{checkRunning ? 'You can continue now. Files checked later follow your choice.' : `OCR will apply to ${count(ocrPolicyCount(snapshot, snapshot.choices.ocr_policy), 'file')}.`}</span><button type="button" className="btn btn-primary" onClick={onContinue}>Continue →</button></StepFooter>
  </section>;
}

function OcrPolicy({ snapshot, checking, onPolicy }: { snapshot: PdfImportSnapshot; checking: boolean; onPolicy(policy: PdfImportChoices['ocr_policy']): void }) {
  const options: Array<{ value: PdfImportChoices['ocr_policy']; label: string; detail: string }> = [
    { value: 'missing_or_suspicious', label: 'Missing or suspicious text', detail: 'Scans, images, and PDFs whose text may be garbled' },
    { value: 'all', label: 'All PDFs and images', detail: 'Read every supported file with OCR' },
    { value: 'none', label: 'None', detail: 'text layers only' },
  ];
  return <fieldset className="pdf-batch-ocr-policy" aria-label="Apply OCR to">
    {options.map((option) => <label key={option.value} className={snapshot.choices.ocr_policy === option.value ? 'active' : ''}>
      <input type="radio" name="pdf-batch-ocr-policy" value={option.value} checked={snapshot.choices.ocr_policy === option.value} onChange={() => onPolicy(option.value)} />
      <span><strong>{option.label}</strong><small>{option.detail}</small></span>
      <b>{ocrPolicyCount(snapshot, option.value).toLocaleString()}{checking ? ' so far' : ''}</b>
    </label>)}
    {snapshot.choices.ocr_policy === 'none' && <p><AlertTriangle size={13} /> Files without a text layer won't be searchable until you run OCR on them.</p>}
  </fieldset>;
}

function FileIssueList({ files }: { files: PdfImportFileSummary[] }) {
  const groups = new Map<string, number>();
  for (const file of files) {
    const message = file.error || file.filename.split('.').pop()?.toUpperCase() || 'Unknown file';
    groups.set(message, (groups.get(message) ?? 0) + 1);
  }
  return <div className="pdf-batch-issue-list"><h3>Files in this group</h3>{[...groups].map(([message, amount]) => <div key={message}><span>{message}</span><b>{amount.toLocaleString()}</b></div>)}</div>;
}

export function DestinationStep({ snapshot, destination, sheets, busy, onDestination, onBack, onContinue }: {
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
  return <section className="pdf-batch-step pdf-batch-destination" aria-label="Review destination">
    <div className="pdf-batch-step-body">
      <header><h2>Review destination</h2><p>Choose where these PDFs belong, then review exactly what will be added.</p></header>
      <fieldset className="pdf-batch-destination-options"><legend>Import into</legend>
        <label className={destination.kind === 'new' ? 'active' : ''}><input type="radio" name="destination" checked={destination.kind === 'new'} onChange={() => onDestination({ kind: 'new', sheet_name: destination.kind === 'new' ? destination.sheet_name : 'Imported PDFs' })} /><span><strong>New sheet</strong><small>Create a sheet for this batch</small></span></label>
        <label className={destination.kind === 'existing' ? 'active' : ''}><input type="radio" name="destination" checked={destination.kind === 'existing'} onChange={() => { const sheet = sheets[0]; if (sheet) onDestination({ kind: 'existing', sheet_id: sheet.id }); }} /><span><strong>Existing sheet</strong><small>Skip matching file names and sizes</small></span></label>
      </fieldset>
      {destination.kind === 'new' ? <label className="pdf-batch-sheet-field">Sheet name<input className="form-input" value={destination.sheet_name} onChange={(event) => onDestination({ kind: 'new', sheet_name: event.currentTarget.value })} /></label>
        : <label className="pdf-batch-sheet-field">Existing sheet<PanelSelect ariaLabel="Existing sheet" topLayer value={destination.sheet_id} onValueChange={(value) => onDestination({ kind: 'existing', sheet_id: Number(value) })}>{sheets.map((sheet) => <option key={sheet.id} value={sheet.id}>{sheet.name}</option>)}</PanelSelect></label>}
      {matching && <div className="pdf-batch-matching" role="status"><LoaderCircle className="spin" size={16} /> <span>Comparing with {sheets.find((sheet) => destination.kind === 'existing' && sheet.id === destination.sheet_id)?.name ?? 'sheet'} · {snapshot.match.checked.toLocaleString()} of {snapshot.match.total.toLocaleString()} checked</span><progress max={Math.max(1, snapshot.match.total)} value={snapshot.match.checked} /></div>}
      <div className="pdf-batch-review-counts">
        <div><strong>{importing.toLocaleString()}</strong><span>{destination.kind === 'existing' ? 'New PDFs to import' : 'PDFs to import'}</span></div>
        {destination.kind === 'existing' && <div><strong>{matched.toLocaleString()}</strong><span>Already in {sheets.find((sheet) => sheet.id === destination.sheet_id)?.name ?? 'sheet'}<small>Same file name and size · Not imported</small></span></div>}
        <div><strong>{ocrPolicyCount(snapshot, snapshot.choices.ocr_policy).toLocaleString()}</strong><span>Read with OCR</span></div>
      </div>
      <CategoryTable snapshot={snapshot} />
    </div>
    <StepFooter><button type="button" className="btn" onClick={onBack}>Back</button><span>{matching ? 'Comparing file names and sizes…' : `${matched.toLocaleString()} existing files will be left unchanged.`}</span><button type="button" className="btn btn-primary" disabled={!destinationIsReady(snapshot, destination) || matching} onClick={onContinue}>{matching ? 'Comparing…' : 'Search setup →'}</button></StepFooter>
  </section>;
}

export function SearchSetupStep({ snapshot, semanticEnabled, consent, busy, importLabel, canImport, batchEstimateBusy, onEstimateOcr, onSemantic, onConsent, onBack, onImport }: {
  snapshot: PdfImportSnapshot;
  semanticEnabled: boolean;
  consent: boolean;
  busy: boolean;
  importLabel: string;
  canImport: boolean;
  batchEstimateBusy: boolean;
  onEstimateOcr(): void;
  onSemantic(enabled: boolean): void;
  onConsent(value: boolean): void;
  onBack(): void;
  onImport(): void;
}) {
  const setup = snapshot.semantic_setup;
  return <section className="pdf-batch-step pdf-batch-search" aria-label="Search setup">
    <div className="pdf-batch-step-body">
      <header><h2>Search setup</h2><p>Keyword search is included. You can also prepare these documents for search by meaning.</p></header>
      <label className="pdf-batch-semantic-choice"><input type="checkbox" checked={semanticEnabled} onChange={(event) => onSemantic(event.currentTarget.checked)} /><span><strong>Search by meaning</strong><small>Find related ideas even when the exact words differ.</small></span></label>
      {semanticEnabled && <div className="pdf-batch-model-setup" data-state={setup.ready ? 'ready' : setup.available ? 'setup' : 'unavailable'}>
        <div><strong>{setup.display_name}</strong><span>{setup.ready ? 'Ready on this device' : setup.runtime_ready ? 'Required for search by meaning' : 'Model and runtime setup required'}</span></div>
        <b>{setup.download_bytes == null ? 'Size not measured' : `About ${bytesLabel(setup.download_bytes)}`}</b>
        {setup.needs_confirmation && <label><input type="checkbox" checked={consent} onChange={(event) => onConsent(event.currentTarget.checked)} /> Download this model when the import starts</label>}
        {!setup.available && <p role="alert">Model setup is unavailable.</p>}
      </div>}
      {ocrPolicyCount(snapshot, snapshot.choices.ocr_policy) > 0 && <div className="pdf-batch-ocr-review" data-state={snapshot.batch_ocr?.state ?? 'idle'}>
        <div><strong>OCR</strong><span>{count(ocrPolicyCount(snapshot, snapshot.choices.ocr_policy), 'file')} · {snapshot.choices.ocr_engine}</span></div>
        {snapshot.batch_ocr?.state === 'ready' && (!snapshot.batch_ocr.needs_confirmation || snapshot.choices.ocr_confirmation)
          ? <b>{snapshot.batch_ocr.needs_confirmation ? 'Cost approved' : 'Ready'}</b>
          : <button type="button" className="btn" disabled={batchEstimateBusy} onClick={onEstimateOcr}>
            {batchEstimateBusy ? 'Checking…' : snapshot.batch_ocr?.needs_confirmation ? 'Review OCR cost' : 'Check OCR setup'}
          </button>}
      </div>}
      <div className="pdf-batch-final-review"><strong>Ready to import</strong><span>Rows begin only after you click Import. OCR and search preparation continue in the shared activity view.</span></div>
    </div>
    <StepFooter><button type="button" className="btn" disabled={busy} onClick={onBack}>Back</button><span>{semanticEnabled ? 'Keyword search and search by meaning will be prepared.' : 'Keyword search will be prepared.'}</span><button type="button" className="btn btn-primary" disabled={!canImport} onClick={onImport}>{busy ? 'Importing…' : importLabel}</button></StepFooter>
  </section>;
}

function StepFooter({ children }: { children: ReactNode }) {
  return <footer className="pdf-batch-footer">{children}</footer>;
}

