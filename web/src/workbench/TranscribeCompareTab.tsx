import { useEffect, useMemo, useRef, useState } from 'react';
import { Loader2, Play, Plus, Settings2, X } from 'lucide-react';

import type { PreviewSampleResult, TranscribeCompareScratchInput } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { EngineOption, TranscribeCompareEngineResult } from '../api/open';
import {
  TRANSCRIBE_ENGINE_FALLBACK,
  engineIsRemote,
  tierForEngine,
  transcribeDiarizationMode,
  transcribeEnginesFromCatalog,
  transcribeOptionsForEngine,
} from '../actions/transcribeEngineCatalog';
import { engineUnavailableReason } from '../actions/engineCatalog';
import { EngineTierBadge } from '../components/EngineTierBadge';
import { MenuPop } from '../components/MenuPop';
import { SegmentedToggle } from '../components/PanelPrimitives';
import { PanelSelect } from '../components/PanelSelect';
import { alignTranscriptSegments } from './transcriptAlign';
import { ConfigureVariantPopover, MediaCompareBody } from './MediaCompareShell';
import { usePaidMediaComparison } from './usePaidMediaComparison';
import { useTranscribeVariantConfigure } from './useTranscribeVariantConfigure';
import {
  VARIANT_PIP_GLYPH,
  useMediaCompareSession,
  variantPip,
  type AlignedUnit,
  type CompareColumn,
  type MediaCompareConfig,
  type MediaNavTarget,
} from './mediaCompareSession';

// Uploaded clips use ordinary accounted preview jobs. The shared comparison
// shell retains timestamp alignment, playback, per-variant errors and voting.

const DEFAULT_ENGINE_IDS = ['faster_whisper', 'parakeet-tdt'];

const DEFAULT_MODEL_SIZE = 'base';
const MODEL_SIZE_PRESETS = ['tiny', 'base', 'small', 'medium', 'large-v3'];
const DEFAULT_VAD = true;

interface TranscribeVariantOptions {
  language: string | undefined;
  modelSize: string | undefined;
  vad: boolean | undefined;
  diarize: boolean | undefined;
}

function optionsOf(column: CompareColumn): TranscribeVariantOptions {
  return {
    language: typeof column.options.language === 'string' ? column.options.language : undefined,
    modelSize:
      typeof column.options.modelSize === 'string' ? column.options.modelSize : undefined,
    vad: typeof column.options.vad === 'boolean' ? column.options.vad : undefined,
    diarize: typeof column.options.diarize === 'boolean' ? column.options.diarize : undefined,
  };
}

/** Which option fields actually take effect for an engine. Unknown engines
 * fail closed; only explicit catalog (or version-skewed built-in fallback)
 * declarations can unlock a control. */
function transcribeFieldsForEngine(engine: EngineOption | undefined): {
  language: boolean;
  modelSize: boolean;
  vad: boolean;
  diarizationMode: 'none' | 'optional' | 'intrinsic';
} {
  const support = transcribeOptionsForEngine(engine);
  return {
    language: support.language,
    modelSize: support.model_size,
    vad: support.vad,
    diarizationMode: transcribeDiarizationMode(engine),
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
      const options = optionsOf(column);
      const fields = transcribeFieldsForEngine(catalogRef.current.find((engine) => engine.id === column.engineId));
      return {
        engine: column.engineId!, time_limit_seconds: limit,
        ...(fields.language ? { language: options.language || null } : {}),
        ...(fields.modelSize ? { model_size: options.modelSize || DEFAULT_MODEL_SIZE } : {}),
        ...(fields.vad ? { vad: options.vad ?? DEFAULT_VAD } : {}),
        ...(fields.diarizationMode === 'optional' ? { diarize: options.diarize ?? false } : {}),
      };
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
  useEffect(() => { if (!active) session.cancelRuns(); }, [active, session.cancelRuns]);
  const {
    catalog,
    columns,
    docs,
    runnableColumns,
    activeDoc,
    engineLabel,
    runningCount,
    sourceOpen,
    setSourceOpen,
    removeColumn,
    updateColumnOptions,
  } = session;
  const quoteInputKey = useMemo(() => JSON.stringify({
    docs: docs.map((doc) => doc.id),
    columns: columns.map(({ id, engineId, options }) => ({ id, engineId, options })),
    timeLimit,
  }), [columns, docs, timeLimit]);

  useEffect(() => {
    paid.clearQuote();
  }, [paid.clearQuote, quoteInputKey]);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const moreInputRef = useRef<HTMLInputElement>(null);
  const {
    configureVariantId,
    setConfigureVariantId,
    popoverShift,
    configureAnchorRef,
    engineSelectRef,
    configurePopoverRef,
    openConfigure,
    onConfigureChooseEngine,
    onConfigureDuplicate,
    addMenuOpen,
    setAddMenuOpen,
    addMenuRef,
    addMenuTriggerRef,
    addMenuPopRef,
    addMenuPos,
    addableEngines,
    onSelectEngine,
    renderSourcePeek,
  } = useTranscribeVariantConfigure(session);

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
      <div
        className="ocr-compare-toolbar"
        data-testid="transcribe-compare-toolbar"
        data-running={runningCount > 0 ? 'true' : 'false'}
      >
        <div className="ocr-compare-controls-row" data-testid="transcribe-compare-controls-row">
          <label className="form-label">First seconds
            <input className="form-input" type="number" min="0.001" step="any"
              data-testid="transcribe-compare-duration" value={timeLimit} disabled={busy}
              onChange={(event) => {
                setTimeLimit(event.target.value);
                for (const column of session.columns) session.invalidateColumn(column.id);
              }} />
          </label>
          <button type="button" className="btn btn-primary" data-testid="transcribe-compare-run"
            disabled={!active || busy || !session.docs.length || !runnableColumns.length || !Number.isFinite(Number(timeLimit)) || Number(timeLimit) <= 0}
            onClick={() => session.pendingPairs.length ? session.runPending() : session.runAll()}>
            <Play size={13} /> {session.preparing ? 'Estimating…' : 'Run comparison'}
          </button>
          {busy && <button type="button" className="btn" data-testid="transcribe-compare-cancel" onClick={session.cancelRuns}>Cancel</button>}
          <button
            type="button"
            className="ocr-compare-source-toggle row-height-select"
            data-testid="transcribe-compare-source-toggle"
            aria-pressed={sourceOpen}
            onClick={() => setSourceOpen((open) => !open)}
          >
            ⊟ Source
          </button>
        </div>

        <fieldset className="ocr-compare-chips-row" data-testid="transcribe-compare-chips-row" disabled={busy}
          style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
          <span className="ocr-compare-scratch-tag" data-testid="transcribe-compare-scratch-tag">
            temporary files · usage is recorded
          </span>
          <span className="ocr-compare-variants-label">ENGINES</span>
          <div className="ocr-compare-variant-chips" data-testid="transcribe-compare-engine-chips">
            {columns.map((column) => {
              const pip = variantPip(column, activeDoc?.runs[column.id]);
              const summary = variantSummary(column);
              const chipEngine = catalog.find((candidate) => candidate.id === column.engineId);
              return (
                <div
                  key={column.id}
                  className="ocr-compare-variant"
                  ref={configureVariantId === column.id ? configureAnchorRef : undefined}
                >
                  <div
                    className="ocr-compare-variant-chip engine-tier-chip"
                    data-testid="transcribe-compare-engine-chip"
                    data-engine-id={column.engineId ?? ''}
                    data-variant-id={column.id}
                    data-pip={pip}
                  >
                    <span className="ocr-compare-pip" data-pip={pip} aria-hidden>
                      {pip === 'running' ? (
                        <Loader2 size={10} className="ocr-compare-spin" />
                      ) : (
                        VARIANT_PIP_GLYPH[pip]
                      )}
                    </span>
                    <button
                      type="button"
                      className="ocr-compare-variant-body"
                      data-testid="transcribe-compare-variant-body"
                      onClick={() => openConfigure(column.id)}
                      title="Configure variant"
                    >
                      <span className="ocr-compare-variant-name">{engineLabel(column.engineId)}</span>
                      {/* Where the chosen engine runs, visible on the chip
                          itself. */}
                      {chipEngine && (
                        <EngineTierBadge
                          tier={tierForEngine(chipEngine)}
                          testId="transcribe-compare-variant-tier"
                        />
                      )}
                      {summary ? (
                        <span className="ocr-compare-variant-summary muted"> · {summary}</span>
                      ) : null}
                    </button>
                    <button
                      type="button"
                      className="ocr-compare-variant-gear"
                      data-testid="transcribe-compare-variant-gear"
                      aria-label={`Configure ${engineLabel(column.engineId)}`}
                      onClick={() => openConfigure(column.id)}
                    >
                      <Settings2 size={12} />
                    </button>
                    <button
                      type="button"
                      className="ocr-compare-variant-remove"
                      data-testid={`transcribe-compare-engine-chip-remove-${column.engineId ?? ''}`}
                      aria-label={`Remove ${engineLabel(column.engineId)}`}
                      onClick={() => {
                        setConfigureVariantId((prev) => (prev === column.id ? null : prev));
                        removeColumn(column.id);
                      }}
                    >
                      <X size={11} />
                    </button>
                  </div>
                  {configureVariantId === column.id ? (
                    <ConfigureVariantPopover
                      testidPrefix="transcribe-compare"
                      popoverRef={configurePopoverRef}
                      style={popoverShift ? { transform: `translateX(${popoverShift}px)` } : undefined}
                      column={column}
                      catalog={catalog}
                      engineSelectRef={engineSelectRef}
                      errorMessage={
                        activeDoc?.runs[column.id]?.status === 'error'
                          ? (activeDoc.runs[column.id] as { message: string }).message
                          : null
                      }
                      onChooseEngine={(engineId) => onConfigureChooseEngine(column.id, engineId)}
                      onDuplicate={() => onConfigureDuplicate(column.id)}
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
                      laterHint="Additional per-engine options stack here as engines declare them."
                    />
                  ) : null}
                </div>
              );
            })}
            <div className="ocr-compare-add-wrap" ref={addMenuRef}>
              <button
                type="button"
                ref={addMenuTriggerRef}
                className="ocr-compare-add-engine engine-tier-chip"
                data-testid="transcribe-compare-add-engine"
                aria-expanded={addMenuOpen}
                onClick={() => setAddMenuOpen((open) => !open)}
              >
                <Plus size={12} /> engine
              </button>
              {addMenuOpen ? (
                <MenuPop
                  ref={addMenuPopRef}
                  className="ocr-compare-add-menu"
                  data-testid="transcribe-compare-add-menu"
                  style={
                    addMenuPos
                      ? { position: 'fixed', inset: 'auto', top: addMenuPos.top, bottom: addMenuPos.bottom, left: addMenuPos.left, right: 'auto', width: addMenuPos.width, margin: 0 }
                      : { position: 'fixed', visibility: 'hidden' }
                  }
                >
                  {addableEngines.length === 0 ? (
                    <div className="ocr-compare-configure-hint muted">All engines added.</div>
                  ) : (
                    addableEngines.map((engine) => {
                      const reason = engineUnavailableReason(engine);
                      return (
                        <button
                          type="button"
                          key={engine.id}
                          className="ocr-compare-add-menu-option"
                          data-testid={`transcribe-compare-add-engine-option-${engine.id}`}
                          data-engine-id={engine.id}
                          role="menuitem"
                          disabled={engine.available === false}
                          onClick={() => onSelectEngine(engine.id)}
                        >
                          {engine.label}
                          <EngineTierBadge
                            tier={tierForEngine(engine)}
                            testId={`transcribe-compare-add-engine-tier-${engine.id}`}
                          />
                          {engineIsRemote(engine) ? ' · billable' : ''}
                          {/* The catalog's own reason (policy or config),
                              never a bare disabled row. */}
                          {reason ? (
                            <span
                              className="engine-tier-unavailable"
                              data-testid={`transcribe-compare-add-engine-reason-${engine.id}`}
                            >
                              {' '}· unavailable — {reason}
                            </span>
                          ) : null}
                        </button>
                      );
                    })
                  )}
                </MenuPop>
              ) : null}
            </div>
          </div>
        </fieldset>
      </div>

      <MediaCompareBody
        session={session}
        config={config}
        testidPrefix="transcribe-compare"
        fileInputRef={fileInputRef}
        moreInputRef={moreInputRef}
        renderSourcePeek={renderSourcePeek}
        columnSummary={variantSummary}
        frontDoorTitle="Drop audio or video clips"
        frontDoorHint="Drop files, choose engines, then run. Each engine receives the same initial interval (600 seconds by default). Shorter clips run in full. Files and transcripts are temporary; paid usage is recorded."
        pendingLabel="transcribing…"
      />
      {paid.quote}
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
  const languageDeclaration = engine?.language;
  const languageOptions = [
    ...(languageDeclaration?.allows_auto === false
      ? []
      : [{ value: '', label: 'Auto' }]),
    ...(languageDeclaration?.choices ?? []),
  ];
  if (
    !fields.language
    && !fields.modelSize
    && !fields.vad
    && fields.diarizationMode === 'none'
  ) {
    return (
      <div className="ocr-compare-configure-hint muted">
        This engine has no configurable options.
      </div>
    );
  }
  return (
    <>
      {fields.diarizationMode === 'optional' && <label className="form-label">
        <input type="checkbox" checked={options.diarize ?? false}
          data-testid="transcribe-compare-configure-diarize" onChange={(event) => onSetDiarize(event.target.checked)} /> Identify speakers
      </label>}
      {fields.diarizationMode === 'intrinsic' ? (
        <div
          className="ocr-compare-configure-hint"
          data-testid="transcribe-compare-diarization-intrinsic"
        >
          Speaker identification is always on for this engine.
        </div>
      ) : null}
      {fields.language ? (
        <label className="ocr-compare-configure-field">
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
      {fields.modelSize ? (
        <div className="ocr-compare-configure-field">
          <span className="ocr-compare-configure-label">Model size</span>
          <div className="ocr-compare-dpi-pills" data-testid="transcribe-compare-model-pills">
            {MODEL_SIZE_PRESETS.map((size) => (
              <button
                type="button"
                key={size}
                className={`ocr-compare-dpi-pill${(options.modelSize ?? DEFAULT_MODEL_SIZE) === size ? ' active' : ''}`}
                data-testid={`transcribe-compare-model-pill-${size}`}
                aria-pressed={(options.modelSize ?? DEFAULT_MODEL_SIZE) === size}
                onClick={() => onSetModelSize(size)}
              >
                {size}
              </button>
            ))}
          </div>
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
