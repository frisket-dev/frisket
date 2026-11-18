import { CompareToolbar } from './CompareLayout';
import { CompareRunControl } from './CompareRunControl';
import { Button } from '../components/Button';
import { TextInput } from '../components/FormControls';
import { useEffect, useMemo, useRef, useState } from 'react';

import type { PreviewSampleResult, TranscribeCompareScratchInput } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { EngineOption, TranscribeCompareEngineResult } from '../api/open';
import {
  TRANSCRIBE_ENGINE_FALLBACK,
  transcribeActiveTarget,
  transcribeActiveTargetOptionReason,
  resolveTranscribeDiarizationMode,
  transcribeDiarizationForEngine,
  transcribeEnginesFromCatalog,
} from '../actions/transcribeEngineCatalog';
import { SegmentedToggle } from '../components/PanelPrimitives';
import { PanelSelect } from '../components/PanelSelect';
import { alignTranscriptSegments } from './transcriptAlign';
import {
  DEFAULT_TRANSCRIBE_MODEL_SIZE,
  DEFAULT_TRANSCRIBE_VAD,
  transcribeCompareInputFor,
  transcribeFieldsForEngine,
  type TranscribeVariantOptions,
} from './transcribeCompareInput';
import { CompareVariantChips, MediaCompareBody } from './MediaCompareShell';
import { usePaidMediaComparison } from './usePaidMediaComparison';
import { useTranscribeVariantConfigure } from './useTranscribeVariantConfigure';
import {
  useMediaCompareSession,
  type AlignedUnit,
  type CompareColumn,
  type MediaCompareConfig,
  type MediaNavTarget,
} from './mediaCompareSession';

// Uploaded clips use ordinary accounted preview jobs. The shared comparison
// shell retains timestamp alignment, playback, per-variant errors and voting.

const DEFAULT_ENGINE_IDS = ['faster_whisper', 'parakeet-tdt'];

const DEFAULT_MODEL_SIZE = DEFAULT_TRANSCRIBE_MODEL_SIZE;
const DEFAULT_VAD = DEFAULT_TRANSCRIBE_VAD;

function optionsOf(column: CompareColumn): TranscribeVariantOptions {
  return {
    language: typeof column.options.language === 'string' ? column.options.language : undefined,
    modelSize:
      typeof column.options.modelSize === 'string' ? column.options.modelSize : undefined,
    vad: typeof column.options.vad === 'boolean' ? column.options.vad : undefined,
    diarize: typeof column.options.diarize === 'boolean' ? column.options.diarize : undefined,
  };
}

function readTranscription(preview: PreviewSampleResult, engine: string): TranscribeCompareEngineResult {
  if (preview.kind !== 'table' || !preview.rows.length) throw new Error('Transcription preview returned no result.');
  const row = preview.rows[0];
  const segments = row.segments?.value;
  return {
    engine, text: String(row.text?.value ?? ''),
    segments: typeof segments === 'string' ? JSON.parse(segments) : segments ?? [],
    detected_language: typeof row.detected_language?.value === 'string' ? row.detected_language.value : null,
    runtime_ms: preview.accounting?.elapsed_ms ?? null,
    warnings: preview.warnings,
  } as TranscribeCompareEngineResult;
}

function createTranscribeConfig(
  runColumn: MediaCompareConfig<TranscribeCompareEngineResult>['runColumn'],
  prepareRun: MediaCompareConfig<TranscribeCompareEngineResult>['prepareRun'],
): MediaCompareConfig<TranscribeCompareEngineResult> {
  return {
  selectorActionId: 'media.transcribe',
  testidPrefix: 'transcribe-compare',
  accept: 'audio/*,video/*',
  classifyFile: (file) => {
    if (file.type.startsWith('video/') || /\.(mp4|webm|mov|mkv)$/i.test(file.name)) return 'video';
    if (file.type.startsWith('audio/') || /\.(wav|mp3|m4a|ogg|flac|aac)$/i.test(file.name))
      return 'audio';
    return null;
  },
  acceptHint: 'Transcribe Compare reads audio and video files.',
  defaultEngineIds: DEFAULT_ENGINE_IDS,
  fallbackCatalog: TRANSCRIBE_ENGINE_FALLBACK,
  enginesFromCatalog: transcribeEnginesFromCatalog,
  docSecondary: (doc) => doc.mediaKind,
  runColumn,
  prepareRun,
  unitsForDoc: (doc, columns): AlignedUnit[] => {
    // Align by SEGMENT INDEX across the columns' transcripts (keyed by column
    // id so each engine retains its own aligned text). No segments anywhere → one
    // paragraph unit with a null seek anchor (the affordance hides).
    const columnIds = columns.map((column) => column.id);
    const segmentsByColumn: Record<string, TranscribeCompareEngineResult['segments'] | undefined> = {};
    const textByColumn: Record<string, string | undefined> = {};
    for (const column of columns) {
      const run = doc.runs[column.id];
      const engineResult = run?.status === 'done' ? run.results : undefined;
      segmentsByColumn[column.id] = engineResult?.segments;
      textByColumn[column.id] = engineResult?.text;
    }
    return alignTranscriptSegments(columnIds, segmentsByColumn, textByColumn).map((unit) => ({
      key: unit.key,
      marker: unit.marker,
      navTarget: { kind: 'seconds', seconds: unit.startSeconds } as MediaNavTarget,
      textByColumn: unit.textByEngine,
    }));
  },
  autoRun: false,
  enableDiff: false,
  includeBillableEngines: true,
  sequential: true,
  noDiffUnitLabel: 'no differences in this segment',
  };
}

interface TranscribeCompareTabProps {
  active?: boolean;
  onSessionChange(state: { hasData: boolean; verdict: string }): void;
}

export function TranscribeCompareTab({ active = true, onSessionChange }: TranscribeCompareTabProps) {
  const { projectApi } = useWorkspaceStores();
  const [timeLimit, setTimeLimit] = useState('600');
  const catalogRef = useRef<EngineOption[]>([]);
  const paid = usePaidMediaComparison<TranscribeCompareEngineResult, TranscribeCompareScratchInput>({
    api: projectApi,
    inputFor: (_doc, column) => {
      const limit = Number(timeLimit);
      if (!Number.isFinite(limit) || limit <= 0) throw new Error('Enter a positive duration in seconds.');
      return transcribeCompareInputFor(
        catalogRef.current.find((engine) => engine.id === column.engineId),
        column.engineId!,
        optionsOf(column),
        limit,
      );
    },
    estimate: (file, input) => projectApi.estimateTranscribeScratch(file, input),
    start: (file, input) => projectApi.compareTranscribeScratch(file, input),
    readResult: readTranscription,
  });
  const config = useMemo(() => createTranscribeConfig(paid.runColumn, paid.prepareRun), [paid.runColumn, paid.prepareRun]);
  const session = useMediaCompareSession(config, onSessionChange);
  useEffect(() => {
    catalogRef.current = session.catalog;
  }, [session.catalog]);
  const busy = session.preparing || session.runProgress !== null;
  const {
    catalog,
    columns,
    docs,
    runnableColumns,
    runningCount,
    sourceOpen,
    setSourceOpen,
    updateColumnOptions,
    cancelRuns,
  } = session;
  useEffect(() => { if (!active) cancelRuns(); }, [active, cancelRuns]);
  const estimateInputKey = useMemo(() => JSON.stringify({
    docs: docs.map((doc) => doc.id),
    columns: columns.map(({ id, engineId, options }) => ({ id, engineId, options })),
    timeLimit,
  }), [columns, docs, timeLimit]);

  const { clearEstimate } = paid;
  useEffect(() => { clearEstimate(); }, [clearEstimate, estimateInputKey]);

  const variantEditor = useTranscribeVariantConfigure(session);

  function variantSummary(column: CompareColumn): string {
    if (!column.engineId) return '';
    const options = optionsOf(column);
    const fields = transcribeFieldsForEngine(
      catalog.find((candidate) => candidate.id === column.engineId),
    );
    const duplicated =
      columns.filter((candidate) => candidate.engineId === column.engineId).length > 1;
    const parts: string[] = [];
    const modelSize = options.modelSize ?? DEFAULT_MODEL_SIZE;
    const vad = options.vad ?? DEFAULT_VAD;
    if (fields.modelSize && (modelSize !== DEFAULT_MODEL_SIZE || duplicated)) {
      parts.push(modelSize);
    }
    if (fields.language && options.language) parts.push(options.language);
    if (fields.vad && vad !== DEFAULT_VAD) parts.push(vad ? 'vad' : 'no-vad');
    return parts.join(' · ');
  }


  return (
    <section className="ocr-compare-tab" data-testid="transcribe-compare-tab">
      {session.selectorOwners}
      <CompareToolbar testIdPrefix="transcribe-compare" running={runningCount > 0} label="Engines" chipsTestId="transcribe-compare-engine-chips" disabled={busy}
        controls={<>
          <label className="form-label form-field">First seconds
            <TextInput type="number" min="0.001" step="any"
              data-testid="transcribe-compare-duration" value={timeLimit} disabled={busy}
              onChange={(event) => {
                setTimeLimit(event.target.value);
                for (const column of session.columns) session.invalidateColumn(column.id);
              }} />
          </label>
          <CompareRunControl testIdPrefix="transcribe-compare" busy={busy}
            disabled={!active || !session.docs.length || !runnableColumns.length || session.runState === 'probing' || !Number.isFinite(Number(timeLimit)) || Number(timeLimit) <= 0}
            workingLabel={session.preparing ? 'Preparing…' : 'Running…'}
            progress={session.runProgress ? `${session.runProgress.done} / ${session.runProgress.total}` : undefined}
            onRun={() => session.pendingPairs.length ? session.runPending() : session.runAll()}
            onCancel={session.cancelRuns} />
          <Button
            type="button"
            className="ocr-compare-source-toggle"
            data-testid="transcribe-compare-source-toggle"
            aria-pressed={sourceOpen}
            onClick={() => setSourceOpen((open) => !open)}
          >
            ⊟ Source
          </Button>
        </>}
      >
        <CompareVariantChips
          session={session}
          editor={variantEditor}
          testidPrefix="transcribe-compare"
          actionId="media.transcribe"
          summary={variantSummary}
          catalogFor={(column) => column.engineId ? catalog : catalog.filter((engine) => !columns.some((variant) => variant.engineId === engine.id))}
          chipTestId="transcribe-compare-engine-chip"
          removeTestId={(column) => `transcribe-compare-engine-chip-remove-${column.engineId ?? ''}`}
          addLabel="Engine"
          addDisabled={busy}
          renderOptionFields={(col) => (
            <TranscribeOptionFields
              column={col}
              engine={catalog.find((candidate) => candidate.id === col.engineId)}
              onSetLanguage={(language) => updateColumnOptions(col.id, { language })}
              onSetModelSize={(modelSize) => updateColumnOptions(col.id, { modelSize })}
              onSetVad={(vad) => updateColumnOptions(col.id, { vad })}
              onSetDiarize={(diarize) => updateColumnOptions(col.id, { diarize })}
            />
          )}
        />
      </CompareToolbar>

      <MediaCompareBody
        session={session}
        config={config}
        testidPrefix="transcribe-compare"
        renderSourcePeek={variantEditor.renderSourcePeek}
        columnSummary={variantSummary}
        frontDoorTitle="Drop audio or video clips"
        frontDoorHint="Drop files, choose engines, then run. Each engine receives the same initial interval (600 seconds by default). Shorter clips run in full. Files and transcripts are temporary; paid usage is recorded."
        pendingLabel="transcribing…"
      />
      {paid.estimateLine}
      {paid.gate}
    </section>
  );
}

// Transcribe's Configure-variant FIELDS (language / model size / VAD, only
// the ones `transcribeFieldsForEngine` declares for the chosen engine) — the
// domain half of the shared ConfigureVariantPopover (MediaCompareShell.tsx);
// the popover chrome (engine select/remote-gate/duplicate footer) is generic
// and reused byte-for-byte from that module, not redefined here — same move
// OcrCompareTab's OcrOptionFields makes for DPI+language.
function TranscribeOptionFields({
  column,
  engine,
  onSetLanguage,
  onSetModelSize,
  onSetVad,
  onSetDiarize,
}: {
  column: CompareColumn;
  engine: EngineOption | undefined;
  onSetLanguage(language: string): void;
  onSetModelSize(modelSize: string): void;
  onSetVad(vad: boolean): void;
  onSetDiarize(diarize: boolean): void;
}) {
  const options = optionsOf(column);
  const fields = transcribeFieldsForEngine(engine);
  const activeTarget = transcribeActiveTarget(engine);
  const modelSizes = activeTarget?.sizes ?? [];
  const hasModelSizeOptions = fields.modelSize && modelSizes.length > 0;
  const targetBindingMissing = Boolean(engine && !activeTarget);
  const semanticDiarization = engine?.diarization;
  const semanticDiarizationMode = resolveTranscribeDiarizationMode(semanticDiarization);
  const activeDiarization = transcribeDiarizationForEngine(engine);
  const targetDiarizationUnavailable = Boolean(
    semanticDiarization?.supported && !activeDiarization?.supported,
  );
  const targetOptionReason = transcribeActiveTargetOptionReason(engine);
  const languageDeclaration = engine?.language;
  const languageOptions = [
    ...(languageDeclaration?.allows_auto === false
      ? []
      : [{ value: '', label: 'Auto' }]),
    ...(languageDeclaration?.choices ?? []),
  ];
  if (
    !fields.language
    && !hasModelSizeOptions
    && !fields.vad
    && fields.diarizationMode === 'none'
    && !targetDiarizationUnavailable
    && !targetBindingMissing
  ) {
    return (
      <div className="ocr-compare-configure-hint muted">
        This engine has no configurable options.
      </div>
    );
  }
  return (
    <>
      {(fields.diarizationMode === 'optional'
        || (targetDiarizationUnavailable && semanticDiarizationMode === 'optional')) && <label className="form-label">
        <input type="checkbox" checked={options.diarize ?? false}
          data-testid="transcribe-compare-configure-diarize"
          disabled={targetDiarizationUnavailable && !options.diarize}
          aria-describedby={targetDiarizationUnavailable ? 'transcribe-compare-diarize-unavailable' : undefined}
          onChange={(event) => {
            if (targetDiarizationUnavailable && event.target.checked) return;
            onSetDiarize(event.target.checked);
          }} /> Identify speakers
      </label>}
      {(targetDiarizationUnavailable || targetBindingMissing) && <p className="ocr-compare-configure-hint muted"
        id="transcribe-compare-diarize-unavailable"
        data-testid="transcribe-compare-diarize-unavailable">
        {targetOptionReason}
      </p>}
      {!targetDiarizationUnavailable && fields.diarizationMode === 'intrinsic' ? (
        <div
          className="ocr-compare-configure-hint"
          data-testid="transcribe-compare-diarization-intrinsic"
        >
          Speaker identification is always on for this engine.
        </div>
      ) : null}
      {fields.language ? (
        <label className="ocr-compare-configure-field form-field">
          <span className="ocr-compare-configure-label">Language</span>
          <PanelSelect
            className="row-height-select"
            data-testid="transcribe-compare-configure-language"
            value={options.language ?? ''}
            onChange={(event) => onSetLanguage(event.target.value)}
          >
            {languageOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </PanelSelect>
        </label>
      ) : null}
      {hasModelSizeOptions ? (
        <div className="ocr-compare-configure-field">
          <span className="ocr-compare-configure-label">Model size</span>
          <SegmentedToggle
            testId="transcribe-compare-model-pills"
            fullWidth={false}
            size="compact"
            ariaLabel="Model size"
            value={options.modelSize ?? DEFAULT_MODEL_SIZE}
            onValueChange={onSetModelSize}
            buttonTestId={(value) => `transcribe-compare-model-pill-${value}`}
            options={modelSizes.map((size) => ({ value: size, label: size, title: size }))}
          />
          <span className="ocr-compare-configure-hint muted">
            bigger = more accurate, slower · default {DEFAULT_MODEL_SIZE}
          </span>
        </div>
      ) : null}
      {fields.vad ? (
        <div className="ocr-compare-configure-field">
          <span className="ocr-compare-configure-label">Voice Activity Detection</span>
          <SegmentedToggle
            testId="transcribe-compare-configure-vad"
            className="ocr-compare-vad-toggle"
            fullWidth={false}
            ariaLabel="Voice Activity Detection"
            value={(options.vad ?? DEFAULT_VAD) ? 'on' : 'off'}
            onValueChange={(value) => onSetVad(value === 'on')}
            buttonTestId={(value) => `transcribe-compare-configure-vad-${value}`}
            options={[
              { value: 'on', label: 'On' },
              { value: 'off', label: 'Off' },
            ]}
          />
          <span className="ocr-compare-configure-hint muted">
            Only transcribe when speech is detected (reduces hallucinations)
          </span>
        </div>
      ) : null}
    </>
  );
}
