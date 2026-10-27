import type { RunEstimate } from '../../api/open';
import type { PdfImportOcrPolicy, PdfImportOcrSelection } from '../../api/pdfImports';
import { PdfOcrPolicy } from '../importWorkspace/pdfBatch/PdfOcrPolicy';
import { SelectorField } from '../../engine-selector/SelectorField';
import { Button } from '../Button';
import { StageStepper } from '../StageStepper';
import { CostGateModal } from '../CostGateModal';
import '../importWorkspace/pdfBatch/PdfBatchImportFlow.css';
import { PdfTextPagePreview } from '../importWorkspace/pdfPacket/PdfTextPagePreview';
import { PanelFooter } from '../PanelLayout';
import { SegmentedToggle } from '../PanelPrimitives';
import { SplitSetupHeader, type SplitSource } from './PdfSplitSetupSections';

export function PdfSplitCheckText({
  projectId, stages, sources, totalSourceCount, sample, samplePage, thumbnailUrl, textSource, selection, policy,
  busy, sampleJob, sampleConsent, quote, primaryLabel, error, onSource, onTextSource,
  onPreview, onRescan, onSelection, onPolicy, onCancelSampleConsent, onConfirmSampleConsent,
  onCancelQuote, onConfirmQuote, onBack, onClose, onContinue,
}: {
  projectId: string; stages: Array<{ id: string; label: string; testId: string }>; sources: SplitSource[]; totalSourceCount: number; sample: SplitSource;
  samplePage: { preview_text?: string | null; preview_truncated?: boolean; thumbnail_url?: string; ocr_text?: string | null; ocr_engine?: string | null } | null;
  thumbnailUrl: string; textSource: 'native' | 'ocr'; selection: PdfImportOcrSelection; policy: PdfImportOcrPolicy;
  busy: boolean; sampleJob: string | null; sampleConsent: RunEstimate | null; quote: { estimate: RunEstimate; pages: number } | null;
  primaryLabel: string; error: string | null;
  onSource(key: string): void; onTextSource(value: 'native' | 'ocr'): void; onPreview(): void; onRescan(): void;
  onSelection(value: PdfImportOcrSelection): void; onPolicy(value: PdfImportOcrPolicy): void;
  onCancelSampleConsent(): void; onConfirmSampleConsent(): void; onCancelQuote(): void; onConfirmQuote(): void;
  onBack(): void; onClose(): void; onContinue(): void;
}) {
  const working = busy || Boolean(sampleJob);
  return <div className="pdf-batch-flow">
    <SplitSetupHeader title="Check text" subtitle={sample.filename} onClose={onClose} />
    {stages.length > 1 && <StageStepper label="Split stages" stages={stages} stage="check" />}
    <div className="panel-body">
      <nav aria-label="Files needing text">
        {sources.map((source) => <Button type="button" key={source.key} disabled={working}
          aria-pressed={source.key === sample.key} onClick={() => onSource(source.key)}>
          {source.filename} · {source.text_category === 'suspicious' ? 'Text may be garbled' : 'No text'}
        </Button>)}
      </nav>
      <SegmentedToggle ariaLabel="Text preview" value={textSource} disabled={working}
        options={[{ value: 'native', label: 'Text layer' }, { value: 'ocr', label: 'OCR preview' }]}
        onValueChange={(value) => value === 'native' ? onTextSource('native') : onPreview()} />
      {samplePage?.ocr_text != null && <Button type="button" disabled={working} onClick={onRescan}>Re-scan</Button>}
      <PdfTextPagePreview page={sample.page_start} thumbnailUrl={samplePage?.thumbnail_url ?? thumbnailUrl}
        text={textSource === 'ocr' ? samplePage?.ocr_text ?? '' : samplePage?.preview_text ?? ''}
        truncated={samplePage?.preview_truncated} loading={!samplePage || Boolean(sampleJob)} />
      <PdfOcrPolicy value={policy} checking={false} busy={working} pdfOnly onChange={onPolicy}
        counts={{ missing: sources.filter((source) => source.text_category === 'missing').length,
          missing_or_suspicious: sources.length, all: totalSourceCount, none: 0 }} />
      <SelectorField projectId={projectId} label="OCR engine" disabled={working}
        query={{ schema_version: 'frisket.selector_choices_query.v1', subject: { kind: 'action', action_id: 'media.ocr', field: 'engine', params: { engine: selection.engine, ...(selection.kind === 'engine_model' ? { model: selection.model } : {}) } } }}
        recentNamespace="pdf-batch-ocr"
        onSelect={(choice) => { const value = choice.authored_selection; if (value.kind === 'engine' || value.kind === 'engine_model') onSelection(value); }}
        onCurrentChoiceChange={(choice) => { const value = choice?.authored_selection; if (value && (value.kind === 'engine' || value.kind === 'engine_model')) onSelection(value); }}
        allowChoice={(choice) => choice.authored_selection.kind === 'engine' || choice.authored_selection.kind === 'engine_model'} />
      {error && <p role="alert">{error}</p>}
      {sampleConsent && <CostGateModal estimate={sampleConsent} message="Apply OCR to this sample page?" onCancel={onCancelSampleConsent} onConfirm={onConfirmSampleConsent} />}
      {quote && <CostGateModal estimate={quote.estimate} message={`Run OCR on ${quote.pages} pages after applying?`} onCancel={onCancelQuote} onConfirm={onConfirmQuote} />}
    </div>
    <PanelFooter><Button type="button" disabled={working} onClick={onBack}>Back</Button><span />
      <Button type="button" className="btn-primary" disabled={working} onClick={onContinue}>{primaryLabel}</Button>
    </PanelFooter>
  </div>;
}
