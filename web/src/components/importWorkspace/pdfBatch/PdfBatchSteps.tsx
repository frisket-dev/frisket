import './PdfBatchImportFlow.css';
import { PanelFrame, PanelBody, PanelFooter } from '../../PanelLayout';
import { Button } from '../../Button';
import { AlertTriangle, CheckCircle2, ExternalLink, FileText, LoaderCircle, Pause, Play, RefreshCw } from 'lucide-react';
import type { ReactNode } from 'react';

import type { PdfImportCategory, PdfImportChoices, PdfImportDestination, PdfImportFileSummary, PdfImportOcrSelection, PdfImportPage, PdfImportSnapshot } from '../../../api/pdfImports';
import type { SelectorChoice } from '../../../api/selectorChoices';
import { SelectorField } from '../../../engine-selector/SelectorField';
import { SegmentedToggle } from '../../PanelPrimitives';
import { PdfTextPagePreview } from '../pdfPacket/PdfTextPagePreview';
import { PdfOcrPolicy, TextSourceBadge } from './PdfOcrPolicy';
import { routePath } from '../../../routes';
import { ActivityProgressCard, ProgressBar } from '../../ActivityProgress';
import { ImportDestinationPicker } from '../ImportDestinationPicker';
import type { PdfBatchDestinationSheet } from './PdfBatchImportFlow';
import { PDF_CATEGORY_LABEL, PDF_CATEGORY_ORDER, PDF_CATEGORY_TONE, PDF_CHECK_CATEGORY_ORDER, canOpenCheckText, count, destinationIsReady, importOutputRows, ocrPolicyCount } from './model';
import { formatBytes } from '../../../format';

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
  return <PanelFrame fill className="pdf-batch-add" aria-label="Add files">
    <PanelBody className="">
      <header><h2>Add files</h2><p>Files are copied to a holding area. Nothing is added to the project until Import.</p></header>
      {snapshot.preflight.state === 'insufficient' ? <div className="panel-error" role="alert">
        <AlertTriangle size={18} />
        <div><strong>Not enough disk space</strong><span>This import needs {formatBytes(uploadBytesTotal)}. {formatBytes(snapshot.preflight.free_bytes ?? 0)} is free.</span></div>
        <Button type="button" disabled={retryingPreflight} onClick={onRetryPreflight}>{retryingPreflight ? 'Checking…' : 'Check again'}</Button>
        <Button type="button" onClick={onCancel}>Choose different files</Button>
      </div> : <><div className="pdf-batch-progress-grid">
        <ProgressCard
          title={reading ? 'Reading folder' : uploadComplete ? 'Copying complete' : 'Copying files'}
          done={snapshot.upload.files_done}
          total={reading ? null : uploadTotal}
          detail={reading
            ? `${count(snapshot.upload.files_done, 'file')} found so far`
            : `${formatBytes(snapshot.upload.bytes_done ?? 0)} of ${formatBytes(uploadBytesTotal)} copied`}
          action={uploadComplete ? <span className="pdf-batch-complete"><CheckCircle2 size={14} /> Complete</span> : <Button type="button" className="btn-quiet" onClick={onPause}>
            {snapshot.paused ? <Play size={14} /> : <Pause size={14} />}{snapshot.paused ? 'Resume' : 'Pause'}
          </Button>}
        />
        <ProgressCard
          title="Checking text layers"
          done={snapshot.analysis.files_done}
          total={checkedTotal || null}
          detail={`${count(snapshot.analysis.files_done, 'file')} checked · ${count(snapshot.upload.files_done, 'file')} copied`}
        />
      </div>
      <p className="pdf-batch-space">{snapshot.preflight.state === 'waiting'
        ? <LoaderCircle className="spin" size={14} aria-hidden />
        : <CheckCircle2 size={14} aria-hidden />}
        {snapshot.preflight.state === 'waiting'
          ? `Checking space for ${formatBytes(uploadBytesTotal)}.`
          : snapshot.preflight.state === 'ready'
            ? `Enough space · needs ${formatBytes(uploadBytesTotal)} · ${formatBytes(snapshot.preflight.free_bytes ?? 0)} free`
            : `More disk space is needed · needs ${formatBytes(uploadBytesTotal)} · ${formatBytes(snapshot.preflight.free_bytes ?? 0)} free`}
      </p></>}
      {needsReselection && <div className="pdf-batch-reselect" role="status">
        <div><strong>Some files still need to be copied</strong><span>Select the same files or folder again. Only pending files are uploaded.</span></div>
        <label className="btn">
          {reselecting ? 'Matching files…' : 'Select files again'}
          <input
            type="file"
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
    </PanelBody>
    <PanelFooter>
      <Button type="button" onClick={onCancel}>Cancel</Button>
      <span>{canOpenCheckText(snapshot)
        ? 'You can review samples while the remaining files are copied.'
        : 'Check text opens once each type has a few files.'}</span>
      <Button type="button" className="btn-primary" disabled={!canOpenCheckText(snapshot)} onClick={onContinue}>Check text →</Button>
    </PanelFooter>
  </PanelFrame>;
}

function ProgressCard({ title, done, total, detail, action }: {
  title: string;
  done: number;
  total: number | null;
  detail: string;
  action?: ReactNode;
}) {
  return <ActivityProgressCard state="active" title={title} footer={action}
    detail={<>{total == null ? `${done.toLocaleString()} found so far` : `${done.toLocaleString()} of ${total.toLocaleString()}`} · {detail}</>}>
    <ProgressBar label={title} completed={done} total={total} state="active" />
  </ActivityProgressCard>;
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
  extensionCounts, unlistedExtensionCount, samplesLoading, page, pageLoading, textSource,
  previewRunning, checkRunning, onCategory, onSample,
  busy, onResample, onTextSource, onPreview, onOcrChoice, onOcrSelection, onPolicy, onOrganization, onBack, onContinue,
}: {
  projectId: string;
  snapshot: PdfImportSnapshot;
  categories: PdfImportCategory[];
  selectedCategory: PdfImportCategory;
  samples: PdfImportFileSummary[];
  extensionCounts: Array<{ extension: string; count: number }>;
  unlistedExtensionCount: number;
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
  onOrganization(organization: PdfImportChoices['organization']): void;
  onBack(): void;
  onContinue(): void;
}) {
  const countFor = snapshot.category_counts[selectedCategory] ?? 0;
  const nonViewer = selectedCategory === 'other' || selectedCategory === 'unreadable';
  const currentOcr = snapshot.choices.ocr_selection;
  const ocrReady = Boolean(page && page.ocr_text !== null && pageUsesSelection(page, currentOcr));
  const shownText = textSource === 'ocr' ? page?.ocr_text ?? '' : page?.native_text ?? '';
  const unreadableCount = snapshot.category_counts.unreadable ?? 0;
  return <PanelFrame fill className="pdf-batch-check" aria-label="Check text">
    <div className="pdf-batch-check-body">
      <aside className="pdf-batch-check-sidebar">
        <h2>Text from your files</h2>
        <p>{count(snapshot.analysis.files_done, 'file')} checked so far. Review representative files before choosing a batch OCR policy.</p>
        {checkRunning && <div className="pdf-batch-analysis-progress" role="status"><LoaderCircle className="spin" size={15} />
          <span>Checking text layers {snapshot.analysis.files_done.toLocaleString()} of {(snapshot.analysis.files_total ?? snapshot.upload.files_done).toLocaleString()}</span>
          <ProgressBar label="Checking text layers" completed={snapshot.analysis.files_done} total={snapshot.analysis.files_total ?? snapshot.upload.files_done} state="active" />
        </div>}
        <div className="pdf-batch-type-list">
          {PDF_CHECK_CATEGORY_ORDER.filter((category) => categories.includes(category)).map((category) => (
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
        <section className="pdf-batch-preview" data-category={selectedCategory} data-testid={selectedCategory === 'image' ? 'pdf-batch-image-preview' : undefined}>
          {nonViewer
            ? <FileIssueList
              category={selectedCategory}
              files={samples}
              extensionCounts={extensionCounts}
              unlistedExtensionCount={unlistedExtensionCount}
              loading={samplesLoading}
            />
            : samplesLoading && !chosenFile
            ? <div className="pdf-batch-empty-preview" role="status"><LoaderCircle className="spin" size={16} /> Loading sample…</div>
            : chosenFile ? <>
            <header><div><strong>{chosenFile.filename}</strong><span>{selectedCategory === 'image' ? 'Image' : chosenFile.page_count == null ? 'Reading pages…' : count(chosenFile.page_count, 'page')}</span></div>
            {selectedCategory === 'image'
              ? <Button type="button" onClick={() => { if (ocrReady) onTextSource('ocr'); else onPreview(); }}>OCR preview</Button>
              : <SegmentedToggle ariaLabel="Text preview" className="segmented-toolbar" fullWidth={false} value={textSource} onValueChange={(value) => {
                if (value === 'ocr' && !ocrReady) onPreview();
                else onTextSource(value as 'native' | 'ocr');
              }} options={[{ value: 'native', label: 'Text layer' }, { value: 'ocr', label: 'OCR preview' }]} />}
            </header>
            {selectedCategory === 'suspicious' && textSource === 'native' && <div className="pdf-batch-garbled"><AlertTriangle size={14} /> This text may be garbled. Try OCR preview to compare.</div>}
            <PdfTextPagePreview
              page={1}
              thumbnailUrl={page?.thumbnail_url ?? chosenFile.thumbnail_url}
              thumbnailAlt={`Page 1 of ${chosenFile.filename}`}
              heading={textSource === 'ocr' ? 'Extracted text' : selectedCategory === 'image' ? 'Text in this image' : 'Text layer in this PDF'}
              text={shownText}
              truncated={textSource === 'native' && Boolean(page?.native_text_truncated)}
              loading={pageLoading || (textSource === 'ocr' && previewRunning)}
              loadingCopy={textSource === 'ocr' ? 'Applying OCR to this page…' : 'Loading preview…'}
              emptyCopy={textSource === 'ocr'
                ? 'Choose OCR preview to read this page.'
                : selectedCategory === 'missing' || selectedCategory === 'image' ? 'This file has no text layer. Try OCR preview.' : 'No text was found on this page.'}
            />
          </> : <div className="pdf-batch-empty-preview">Select a sample file.</div>}
        </section>
        <div className="pdf-batch-ocr-controls">
          <div className="pdf-batch-ocr-controls-head"><strong>Apply OCR to</strong></div>
          <PdfOcrPolicy value={snapshot.choices.ocr_policy} checking={checkRunning} busy={busy} onChange={onPolicy}
            counts={{ missing: ocrPolicyCount(snapshot, 'missing'), missing_or_suspicious: ocrPolicyCount(snapshot, 'missing_or_suspicious'), all: ocrPolicyCount(snapshot, 'all'), none: 0 }} />
          <div className="pdf-batch-engine">
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
          </div>
          <PdfOrganizationChoice snapshot={snapshot} busy={busy} onOrganization={onOrganization} />
        </div>
      </div>
    </div>
    <PanelFooter><Button type="button" disabled={busy} onClick={onBack}>Back</Button><span>{snapshot.organization_summary?.pdf_count
      ? snapshot.choices.organization === 'split_documents'
        ? `Find documents in ${count(snapshot.organization_summary.pdf_count, 'PDF')} next`
        : `${count(snapshot.upload.files_total, 'file')} → ${snapshot.organization_summary.output_rows == null ? '… rows' : count(snapshot.organization_summary.output_rows, 'row')}`
      : checkRunning ? 'You can continue now. Files checked later follow your choice.' : `OCR will apply to ${count(ocrPolicyCount(snapshot, snapshot.choices.ocr_policy), 'file')}${unreadableCount > 0 ? ` · ${unreadableCount.toLocaleString()} couldn't be read` : ''}.`}</span><Button type="button" className="btn-primary" disabled={busy} onClick={onContinue}>{busy ? 'Saving…' : snapshot.choices.organization === 'split_documents' ? 'Find documents →' : 'Continue →'}</Button></PanelFooter>
  </PanelFrame>;
}

function PdfOrganizationChoice({ snapshot, busy, onOrganization }: {
  snapshot: PdfImportSnapshot;
  busy: boolean;
  onOrganization(value: PdfImportChoices['organization']): void;
}) {
  const summary = snapshot.organization_summary;
  const pdfCount = summary?.pdf_count ?? (snapshot.category_counts.text ?? 0)
    + (snapshot.category_counts.suspicious ?? 0) + (snapshot.category_counts.missing ?? 0);
  if (pdfCount === 0) return null;
  const organization = snapshot.choices.organization ?? 'keep_documents';
  const total = snapshot.upload.files_total;
  const options = [
    { value: 'keep_documents', label: 'Keep documents', detail: `${count(pdfCount, 'row')}, one per PDF` },
    { value: 'pages', label: 'One row per page', detail: summary ? count(summary.page_count, 'row') : 'Each PDF page becomes a row' },
    { value: 'split_documents', label: 'Split into documents', detail: 'Find where documents start' },
  ] as const;
  return <fieldset className="pdf-batch-organization" aria-label="PDFs">
    <legend>PDFs <span>{pdfCount} of {total} files</span></legend>
    <div className="pdf-batch-ocr-policy">{options.map((option) => <label key={option.value} className={organization === option.value ? 'active' : ''}>
      <input type="radio" name="pdf-batch-organization" value={option.value}
        checked={organization === option.value}
        disabled={busy || (option.value === 'split_documents' && summary?.split_supported === false)}
        onChange={() => onOrganization(option.value)} />
      <span><strong>{option.label}</strong><small>{option.detail}</small>
        {option.value === 'split_documents' && summary?.split_limit_reason && <small>{summary.split_limit_reason}</small>}
      </span>
    </label>)}</div>
  </fieldset>;
}

function FileIssueList({ category, files, extensionCounts, unlistedExtensionCount, loading }: {
  category: PdfImportCategory;
  files: PdfImportFileSummary[];
  extensionCounts: Array<{ extension: string; count: number }>;
  unlistedExtensionCount: number;
  loading: boolean;
}) {
  if (category === 'other') {
    return <div className="pdf-batch-issue-list">
      <h3>Other files</h3>
      <p>Imported as files. Their contents aren’t expanded into tables or read with OCR.</p>
      {loading
        ? <p role="status"><LoaderCircle className="spin" size={15} /> Loading file details…</p>
        : <>
          {extensionCounts.map(({ extension, count: extensionCount }) => <div key={extension || '(none)'}>
            <strong>{extension || 'No file extension'}</strong>
            <span>{count(extensionCount, 'file')}</span>
          </div>)}
          {unlistedExtensionCount > 0 && <div><strong>Other extensions</strong><span>{count(unlistedExtensionCount, 'file')}</span></div>}
          {extensionCounts.length === 0 && files.map((file) => <div key={file.occurrence_id}>
            <strong>{file.filename}</strong><span>Imported as a file</span>
          </div>)}
        </>}
    </div>;
  }
  return <div className="pdf-batch-issue-list">
    <h3>Couldn't read these files</h3>
    {loading
      ? <p role="status"><LoaderCircle className="spin" size={15} /> Loading file details…</p>
      : files.length > 0
        ? files.map((file) => <div key={file.occurrence_id}>
          <strong>{file.filename}</strong>
          <span>Couldn’t read this file. It will be imported without text.</span>
        </div>)
        : <p>No file details are available. These files will be imported without text.</p>}
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
  const outputRows = importOutputRows(snapshot);
  const expanded = outputRows !== null && outputRows !== importing;
  const sheetName = destination.kind === 'existing'
    ? sheets.find((sheet) => sheet.id === destination.sheet_id)?.name ?? 'this sheet'
    : destination.sheet_name.trim() || 'the new sheet';
  const reviewCounts = destination.kind === 'existing' && snapshot.match.state === 'ready'
    ? snapshot.match.new_category_counts ?? snapshot.category_counts
    : snapshot.category_counts;
  return <PanelFrame fill className="pdf-batch-destination" aria-label="Review destination">
    <PanelBody className="">
      <ImportDestinationPicker destination={destination} sheets={sheets}
        defaultSheetName="Imported files" busy={busy} onDestination={onDestination} />
      {destination.kind === 'existing' && <p className="form-hint">Skip matching file names and sizes.</p>}
      {matching && <div className="pdf-batch-matching" role="status"><LoaderCircle className="spin" size={16} /> <span>Comparing with {sheets.find((sheet) => destination.kind === 'existing' && sheet.id === destination.sheet_id)?.name ?? 'sheet'} · {snapshot.match.checked.toLocaleString()} of {snapshot.match.total.toLocaleString()} checked</span><ProgressBar label="Comparing existing rows" completed={snapshot.match.checked} total={snapshot.match.total} state="active" /></div>}
      <p className="pdf-batch-destination-summary"><strong>{outputRows === null ? 'Calculating rows…' : expanded ? `${count(outputRows, 'row')} from ${count(importing, 'file')}` : count(importing, destination.kind === 'existing' ? 'new file' : 'file')}</strong>{destination.kind === 'existing' && matched > 0 && <span>{count(matched, 'file')} already in {sheetName} and left as is</span>}</p>
      <div className="pdf-batch-destination-table" role="table" aria-label="File import review">
        <div role="row" className="pdf-batch-destination-heading"><strong role="columnheader">Type</strong><strong role="columnheader">Files</strong><strong role="columnheader">Rows</strong><strong role="columnheader">Text</strong></div>
        {destination.kind === 'existing' && matched > 0 && <div role="row" className="pdf-batch-existing-row">
          <span role="cell"><strong>Already in {sheetName}</strong><small>Same file name and size</small></span>
          <b role="cell">{matched.toLocaleString()}</b>
          <span role="cell">—</span>
          <span role="cell"><i className="pdf-batch-text-badge neutral">Not imported</i><a className="link-btn" href={routePath({ kind: 'project', projectId, sheetId: String(destination.sheet_id) })} target="_blank" rel="noopener noreferrer" title="Open sheet in a new tab">View sheet <ExternalLink size={11} /></a></span>
        </div>}
        {PDF_CATEGORY_ORDER.filter((category) => category !== 'unchecked' && (reviewCounts[category] ?? 0) > 0).map((category) => <div role="row" key={category}>
          <span role="cell">{PDF_CATEGORY_LABEL[category]}</span>
          <b role="cell">{(reviewCounts[category] ?? 0).toLocaleString()}</b>
          <CategoryRows snapshot={snapshot} category={category} files={reviewCounts[category] ?? 0} />
          <TextSourceBadge category={category} policy={snapshot.choices.ocr_policy} />
        </div>)}
      </div>
    </PanelBody>
    <PanelFooter><Button type="button" disabled={busy} onClick={onBack}>Back</Button><span>{matching
      ? 'Comparing file names and sizes…'
      : destination.kind === 'new' ? `Creates “${sheetName}”.` : `New rows are added at the end of ${sheetName}.`}</span><Button type="button" className="btn-primary" disabled={busy || !destinationIsReady(snapshot, destination) || matching} onClick={onContinue}>{matching ? 'Comparing…' : busy ? 'Saving…' : 'Search setup →'}</Button></PanelFooter>
  </PanelFrame>;
}

function CategoryRows({ snapshot, category, files }: { snapshot: PdfImportSnapshot; category: PdfImportCategory; files: number }) {
  const pdf = category === 'text' || category === 'suspicious' || category === 'missing';
  const summary = snapshot.organization_summary;
  const categoryRows = snapshot.choices.destination.kind === 'existing' && snapshot.match.state === 'ready'
    ? summary?.new_category_rows : summary?.category_rows;
  const rows = categoryRows?.[category]
    ?? (pdf && snapshot.choices.organization !== 'keep_documents' && snapshot.choices.organization != null ? null : files);
  return <span role="cell">{rows == null ? '…' : pdf ? count(rows, snapshot.choices.organization === 'pages' ? 'page' : 'document') : rows.toLocaleString()}</span>;
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
  return <PanelFrame fill className="pdf-batch-search" aria-label="Search setup">
    <PanelBody className="">
      <header><h2>Search setup</h2><p>Keyword search is included. You can also prepare these documents for search by meaning.</p></header>
      <label className="pdf-batch-semantic-choice"><input type="checkbox" disabled={busy} checked={semanticEnabled} onChange={(event) => onSemantic(event.currentTarget.checked)} /><span><strong>Search by meaning</strong><small>Find related ideas even when the exact words differ.</small></span></label>
      {semanticEnabled && <div className="pdf-batch-model-setup" data-state={setup.ready ? 'ready' : setup.available ? 'setup' : 'unavailable'}>
        <div><strong>{setup.display_name}</strong><span>{setup.ready ? 'Ready on this device' : setup.runtime_ready ? 'Required for search by meaning' : 'Model and runtime setup required'}</span></div>
        <b>{setup.download_bytes == null ? 'Size not measured' : `About ${formatBytes(setup.download_bytes)}`}</b>
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
              ? <Button type="button" title={batchEstimateError ?? snapshot.batch_ocr?.error ?? undefined} disabled={batchEstimateBusy} onClick={onEstimateOcr}>{batchEstimateBusy ? 'Checking…' : 'Try again'}</Button>
              : <span className="pdf-batch-inline-progress"><LoaderCircle className="spin" size={13} /> Checking…</span>}
      </div>}
      {snapshot.paused && <p className="pdf-batch-paused-import" role="status">Copying is paused. Go back to Add files to resume before importing.</p>}
    </PanelBody>
    <PanelFooter><Button type="button" disabled={busy} onClick={onBack}>Back</Button><span>{snapshot.paused
      ? 'Copying is paused. Go back to Add files to resume.'
      : semanticWaitingForConsent
      ? `Allow the ${setup.download_bytes == null ? 'model' : formatBytes(setup.download_bytes)} download, or turn off search by meaning.`
      : semanticEnabled ? 'Keyword search and search by meaning will be prepared.' : 'Keyword search will be prepared.'}</span><Button type="button" className="btn-primary" disabled={!canImport} onClick={onImport}>{busy ? 'Importing…' : importLabel}</Button></PanelFooter>
  </PanelFrame>;
}

