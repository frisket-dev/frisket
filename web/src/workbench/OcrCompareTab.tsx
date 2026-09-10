import { useEffect, useMemo, useRef } from 'react';
import { Loader2, Play, Plus, RotateCw, Settings2, X } from 'lucide-react';

import type {
  OcrComparePreviewBlock,
  OcrComparePreviewPageResult,
  OcrCompareScratchInput,
  PreviewOverlayCell,
  PreviewSampleResult,
} from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { OcrCompareTarget } from '../actions/ocrCompare';
import { OCR_ENGINE_FALLBACK, ocrEnginesFromCatalog, tierForEngine } from '../actions/engineCatalog';
import { EngineTierBadge } from '../components/EngineTierBadge';
import { SegmentedToggle } from '../components/PanelPrimitives';
import { PanelSelect } from '../components/PanelSelect';
import { ConfigureVariantPopover, MediaCompareBody } from './MediaCompareShell';
import { useOcrVariantConfigure } from './useOcrVariantConfigure';
import { usePaidMediaComparison } from './usePaidMediaComparison';
import {
  MEDIA_LANGUAGE_OPTIONS,
  VARIANT_PIP_GLYPH,
  useMediaCompareSession,
  variantPip,
  type AlignedUnit,
  type CompareColumn,
  type MediaCompareConfig,
  type MediaNavTarget,
  type ScratchDoc,
} from './mediaCompareSession';

export type { OcrCompareTarget } from '../actions/ocrCompare';

// Upload-first OCR comparison: each column is an engine/options variant over
// disposable dropped media. Adding or editing stages work until Run.

const DEFAULT_ENGINE_IDS = ['rapidocr', 'dots.mocr'];
const MAX_SAMPLE_PAGES = 10;
const DEFAULT_DPI = 200;
const DPI_PRESETS = [100, 200, 300];
const DPI_MIN = 50;
const DPI_MAX = 600;
const clampDpi = (dpi: number) => Math.max(DPI_MIN, Math.min(DPI_MAX, Math.round(dpi)));

interface OcrVariantOptions {
  dpi: number;
  language: string;
  pagesText: string;
  searchablePdf: boolean;
}

function optionsOf(column: CompareColumn): OcrVariantOptions {
  return {
    dpi: typeof column.options.dpi === 'number' ? column.options.dpi : DEFAULT_DPI,
    language: typeof column.options.language === 'string' ? column.options.language : '',
    pagesText: typeof column.options.pagesText === 'string' ? column.options.pagesText : '1',
    searchablePdf: column.options.searchablePdf === true,
  };
}

function parsePageList(value: string): { pages: number[]; error: string | null } {
  if (!/^\s*[1-9]\d*\s*(,\s*[1-9]\d*\s*)*$/.test(value)) {
    return { pages: [], error: 'Enter a comma-separated list of positive page numbers.' };
  }
  const parts = value.split(',');
  if (parts.length > MAX_SAMPLE_PAGES) {
    return { pages: [], error: `Choose at most ${MAX_SAMPLE_PAGES} pages.` };
  }
  return { pages: parts.map((part) => Number(part.trim())), error: null };
}

function cellValue(row: Record<string, PreviewOverlayCell>, name: string): unknown {
  return row[name]?.value;
}

function previewPages(preview: PreviewSampleResult, engineId: string): OcrComparePreviewPageResult[] {
  if (preview.kind !== 'table') return [];
  return preview.rows.map((row, index) => {
    const rawBlocks = cellValue(row, 'blocks');
    let blocks: OcrComparePreviewBlock[] = [];
    if (Array.isArray(rawBlocks)) blocks = rawBlocks as OcrComparePreviewBlock[];
    else if (typeof rawBlocks === 'string') {
      try {
        const parsed: unknown = JSON.parse(rawBlocks);
        if (Array.isArray(parsed)) blocks = parsed as OcrComparePreviewBlock[];
      } catch {
        blocks = [];
      }
    }
    const page = cellValue(row, 'page');
    const text = cellValue(row, 'text');
    return {
      page: typeof page === 'number' ? page : index + 1,
      engine: engineId,
      text: typeof text === 'string' ? text : '',
      blocks,
      warnings: [],
      errors: [],
      runtime_ms: null,
    };
  });
}

function doneTextForPage(
  doc: ScratchDoc<OcrComparePreviewPageResult[]>,
  columnId: string,
  page: number,
): string {
  const run = doc.runs[columnId];
  if (run?.status !== 'done') return '';
  return run.results.find((item) => item.page === page)?.text ?? '';
}

function inputFor(
  _doc: ScratchDoc<OcrComparePreviewPageResult[]>,
  column: CompareColumn,
): OcrCompareScratchInput {
  const options = optionsOf(column);
  const parsedPages = parsePageList(options.pagesText);
  if (parsedPages.error) throw new Error(parsedPages.error);
  return {
    engine: column.engineId as string,
    pages: parsedPages.pages,
    language: options.language || null,
    dpi: options.dpi,
    searchable_pdf: options.searchablePdf,
  };
}

// The OCR shell config: uploaded PDF/image media, source peek, page-marker
// alignment, and staged per-variant paid-aware preview calls.
function createOcrConfig(
  paid: Pick<
    MediaCompareConfig<OcrComparePreviewPageResult[]>,
    'prepareRun' | 'runColumn'
  >,
  defaultEngineIds: string[],
  defaultColumnOptions: Record<string, unknown>,
): MediaCompareConfig<OcrComparePreviewPageResult[]> {
  return {
  testidPrefix: 'ocr-compare',
  accept: 'application/pdf,image/*,.pdf,.png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp',
  classifyFile: (file) => {
    if (file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')) return 'pdf';
    if (file.type.startsWith('image/') || /\.(png|jpe?g|webp|tiff?|bmp)$/i.test(file.name)) return 'image';
    return null;
  },
  acceptHint: 'OCR Compare reads PDF and image files.',
  defaultEngineIds,
  fallbackCatalog: OCR_ENGINE_FALLBACK,
  enginesFromCatalog: ocrEnginesFromCatalog,
  defaultColumnOptions,
  docSecondary: (doc) =>
    doc.mediaKind === 'pdf' ? 'PDF · page 1 by default' : 'image',
  ...paid,
  unitsForDoc: (doc, columns): AlignedUnit[] => {
    const pages = [...new Set(columns.flatMap((column) => {
      const run = doc.runs[column.id];
      if (run?.status === 'done') return run.results.map((result) => result.page);
      const parsed = parsePageList(optionsOf(column).pagesText);
      return parsed.error ? [1] : parsed.pages;
    }))].sort((a, b) => a - b);
    return pages.map((page) => {
      const textByColumn: Record<string, string> = {};
      for (const column of columns) textByColumn[column.id] = doneTextForPage(doc, column.id, page);
      return {
        key: `page-${page}`,
        marker: pages.length > 1 ? `Page ${page}` : null,
        navTarget: { kind: 'page', page } as MediaNavTarget,
        textByColumn,
      };
    });
  },
  autoRun: false,
  sequential: true,
  includeBillableEngines: true,
  noDiffUnitLabel: 'no differences on this page',
  };
}

interface OcrCompareTabProps {
  target?: OcrCompareTarget | null;
  active?: boolean;
  onSessionChange(state: { hasData: boolean; verdict: string }): void;
}

export function OcrCompareTab({ target = null, active = true, onSessionChange }: OcrCompareTabProps) {
  const { projectApi } = useWorkspaceStores();
  const paid = usePaidMediaComparison<OcrComparePreviewPageResult[], OcrCompareScratchInput>({
    api: projectApi,
    inputFor,
    estimate: (file, input) => projectApi.estimateOcrScratch(file, input),
    start: (file, input) => projectApi.compareOcrScratch(file, input),
    readResult: previewPages,
  });
  const launchEngine = target?.engine ?? null;
  const defaultEngineIds = useMemo(
    () => launchEngine
      ? [launchEngine, ...DEFAULT_ENGINE_IDS.filter((id) => id !== launchEngine)].slice(0, 2)
      : DEFAULT_ENGINE_IDS,
    [launchEngine],
  );
  const defaultColumnOptions = useMemo(() => ({
    dpi: clampDpi(target?.dpi ?? DEFAULT_DPI),
    language: target?.language ?? '',
    pagesText: '1',
    searchablePdf: target?.searchable_pdf ?? false,
  }), [target?.dpi, target?.language, target?.searchable_pdf]);
  const config = useMemo(
    () => createOcrConfig(
      { prepareRun: paid.prepareRun, runColumn: paid.runColumn },
      defaultEngineIds,
      defaultColumnOptions,
    ),
    [defaultColumnOptions, defaultEngineIds, paid.prepareRun, paid.runColumn],
  );
  const session = useMediaCompareSession(config, onSessionChange);
  const {
    catalog,
    columns,
    docs,
    runnableColumns,
    activeDoc,
    engineLabel,
    effectiveMode,
    setMode,
    runState,
    runProgress,
    runningCount,
    pendingPairs,
    runPending,
    runAll,
    sourceOpen,
    setSourceOpen,
    wrapText,
    setWrapText,
    updateColumnOptions,
    preparing,
    cancelRuns,
  } = session;
  const busy = preparing || runProgress !== null;
  const quoteInputKey = useMemo(() => JSON.stringify({
    docs: docs.map((doc) => doc.id),
    columns: columns.map(({ id, engineId, options }) => ({ id, engineId, options })),
  }), [columns, docs]);

  useEffect(() => {
    if (!active) cancelRuns();
    return () => cancelRuns();
  }, [active, cancelRuns]);

  useEffect(() => {
    paid.clearQuote();
  }, [paid.clearQuote, quoteInputKey]);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const moreInputRef = useRef<HTMLInputElement>(null);
  const {
    configureVariantId,
    popoverShift,
    configureAnchorRef,
    engineSelectRef,
    configurePopoverRef,
    openConfigure,
    addEngineFlow,
    onChooseEngine,
    onRemoveVariant,
    onDuplicate,
    renderSourcePeek,
  } = useOcrVariantConfigure(session);


  function variantSummary(column: CompareColumn): string {
    if (!column.engineId) return '';
    const options = optionsOf(column);
    const duplicated =
      columns.filter((candidate) => candidate.engineId === column.engineId).length > 1;
    const parts: string[] = [];
    if (options.dpi !== DEFAULT_DPI || duplicated) parts.push(`${options.dpi}dpi`);
    if (options.language) parts.push(options.language);
    if (options.pagesText.trim() !== '1') parts.push(`p${options.pagesText}`);
    if (options.searchablePdf) parts.push('searchable PDF');
    return parts.join(' · ');
  }

  const runLabel =
    runState === 'running'
      ? `Running · ${runProgress?.done ?? 0} / ${runProgress?.total ?? runningCount}`
      : runState === 'probing'
        ? 'Reading pages…'
        : runState === 'fresh'
          ? 'Re-run'
          : 'Run';

  return (
    <section className="ocr-compare-tab" data-testid="ocr-compare-tab">
      <div
        className="ocr-compare-toolbar"
        data-testid="ocr-compare-toolbar"
        data-running={runningCount > 0 ? 'true' : 'false'}
      >
        <div className="ocr-compare-controls-row" data-testid="ocr-compare-controls-row">
          <button
            type="button"
            className={`btn ocr-compare-run${runState === 'pending' ? ' btn-primary' : ''}`}
            data-testid="ocr-compare-run"
            data-run-state={runState}
            data-pending-count={pendingPairs.length}
            disabled={!active || (!busy && (runState === 'disabled' || runState === 'probing'))}
            title={
              runState === 'disabled'
                ? 'Add an engine to stage work'
                : runState === 'probing'
                  ? 'Reading page count — Run available once the sample is ready'
                  : runState === 'fresh'
                    ? 'Re-run all variants'
                    : undefined
            }
            onClick={() => {
              if (busy) cancelRuns();
              else if (runState === 'pending') runPending();
              else if (runState === 'fresh') runAll();
            }}
          >
            {busy ? (
              <Loader2 size={13} className="ocr-compare-spin" aria-hidden />
            ) : runState === 'fresh' ? (
              <RotateCw size={13} aria-hidden />
            ) : (
              <Play size={13} aria-hidden />
            )}
            <span className="ocr-compare-run-label">{busy ? 'Cancel' : runLabel}</span>
            {runState === 'pending' ? (
              <span className="ocr-compare-run-count mono"> · {pendingPairs.length} pending</span>
            ) : null}
            {runState === 'running' ? (
              <span className="ocr-compare-run-progress" data-testid="ocr-compare-run-progress" />
            ) : null}
          </button>
          <SegmentedToggle
            testId="ocr-compare-mode-switch"
            className="ocr-compare-mode-switch"
            fullWidth={false}
            ariaLabel="Diff or survey view"
            value={effectiveMode}
            onValueChange={(value) => setMode(value as 'diff' | 'survey')}
            buttonTestId={(value) => `ocr-compare-mode-${value}`}
            options={[
              {
                value: 'diff',
                label: '◨ Diff',
                disabledReason:
                  runnableColumns.length !== 2 ? 'Diff needs exactly two variants' : undefined,
              },
              { value: 'survey', label: '◫ Survey' },
            ]}
          />
          <button
            type="button"
            className="ocr-compare-source-toggle row-height-select"
            data-testid="ocr-compare-source-toggle"
            aria-pressed={sourceOpen}
            onClick={() => setSourceOpen((open) => !open)}
          >
            ⊟ Source
          </button>
          <button
            type="button"
            className="ocr-compare-wrap-toggle row-height-select"
            data-testid="ocr-compare-wrap-toggle"
            aria-pressed={wrapText}
            title={wrapText ? 'Wrap text to the panel (on)' : 'No wrap — scroll horizontally'}
            onClick={() => setWrapText((wrap) => !wrap)}
          >
            ⏎ Wrap
          </button>
        </div>

        <div className="ocr-compare-chips-row" data-testid="ocr-compare-chips-row">
          <span className="ocr-compare-scratch-tag" data-testid="ocr-compare-scratch-tag">
            scratch · not in a project
          </span>
          <span className="ocr-compare-variants-label">VARIANTS</span>
          <div className="ocr-compare-variant-chips" data-testid="ocr-compare-variant-chips">
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
                    data-testid="ocr-compare-variant-chip"
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
                      data-testid="ocr-compare-variant-body"
                      disabled={busy}
                      onClick={() => openConfigure(column.id)}
                      title="Configure variant"
                    >
                      <span className="ocr-compare-variant-name">{engineLabel(column.engineId)}</span>
                      {chipEngine && (
                        <EngineTierBadge
                          tier={tierForEngine(chipEngine)}
                          testId="ocr-compare-variant-tier"
                        />
                      )}
                      {summary ? (
                        <span className="ocr-compare-variant-summary muted"> · {summary}</span>
                      ) : null}
                    </button>
                    <button
                      type="button"
                      className="ocr-compare-variant-gear"
                      data-testid="ocr-compare-variant-gear"
                      aria-label={`Configure ${engineLabel(column.engineId)}`}
                      disabled={busy}
                      onClick={() => openConfigure(column.id)}
                    >
                      <Settings2 size={12} />
                    </button>
                    <button
                      type="button"
                      className="ocr-compare-variant-remove"
                      data-testid="ocr-compare-variant-remove"
                      aria-label={`Remove ${engineLabel(column.engineId)}`}
                      disabled={busy}
                      onClick={() => onRemoveVariant(column.id)}
                    >
                      <X size={11} />
                    </button>
                  </div>
                  {configureVariantId === column.id ? (
                    <ConfigureVariantPopover
                      testidPrefix="ocr-compare"
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
                      onChooseEngine={(engineId) => onChooseEngine(column.id, engineId)}
                      onDuplicate={() => onDuplicate(column.id)}
                      renderOptionFields={(col) => (
                        <OcrOptionFields
                          column={col}
                          onSetDpi={(dpi) => updateColumnOptions(col.id, { dpi })}
                          onSetLanguage={(language) => updateColumnOptions(col.id, { language })}
                          onSetPagesText={(pagesText) => updateColumnOptions(col.id, { pagesText })}
                          onSetSearchablePdf={(searchablePdf) =>
                            updateColumnOptions(col.id, { searchablePdf })
                          }
                          disabled={busy}
                        />
                      )}
                      laterHint="More per-engine options (model tier, page range, deskew, table mode) stack here as engines declare them."
                    />
                  ) : null}
                </div>
              );
            })}
            <button
              type="button"
              className="ocr-compare-add-engine engine-tier-chip"
              data-testid="ocr-compare-add-engine"
              disabled={busy}
              onClick={addEngineFlow}
            >
              <Plus size={12} /> engine
            </button>
          </div>
        </div>
      </div>

      <MediaCompareBody
        session={session}
        config={config}
        testidPrefix="ocr-compare"
        fileInputRef={fileInputRef}
        moreInputRef={moreInputRef}
        renderSourcePeek={renderSourcePeek}
        columnSummary={variantSummary}
        frontDoorTitle="Drop a small sample of PDFs or images"
        frontDoorHint="Nothing uploads to your project — this session is disposable. Configure variants, then Run."
        pendingLabel="not run yet — press Run"
      />
      {paid.quote}
      {paid.gate}
    </section>
  );
}

// OCR's Configure-variant FIELDS (rendering DPI + language) — the domain half
// of the shared ConfigureVariantPopover (MediaCompareShell.tsx); the popover
// chrome itself (engine select and duplicate footer) is generic and
// reused byte-for-byte from that module, not redefined here.
function OcrOptionFields({
  column,
  onSetDpi,
  onSetLanguage,
  onSetPagesText,
  onSetSearchablePdf,
  disabled,
}: {
  column: CompareColumn;
  onSetDpi(dpi: number): void;
  onSetLanguage(language: string): void;
  onSetPagesText(pagesText: string): void;
  onSetSearchablePdf(searchablePdf: boolean): void;
  disabled: boolean;
}) {
  const options = optionsOf(column);
  const pagesProblem = parsePageList(options.pagesText).error;
  return (
    <fieldset disabled={disabled}>
      <div className="ocr-compare-configure-field">
        <span className="ocr-compare-configure-label">Rendering DPI</span>
        <div className="ocr-compare-dpi-pills">
          {DPI_PRESETS.map((dpi) => (
            <button
              type="button"
              key={dpi}
              className={`ocr-compare-dpi-pill${options.dpi === dpi ? ' active' : ''}`}
              data-testid={`ocr-compare-dpi-pill-${dpi}`}
              aria-pressed={options.dpi === dpi}
              onClick={() => onSetDpi(dpi)}
            >
              {dpi}
            </button>
          ))}
          <input
            type="number"
            className="ocr-compare-dpi-stepper"
            data-testid="ocr-compare-dpi-stepper"
            min={DPI_MIN}
            max={DPI_MAX}
            step={1}
            value={options.dpi}
            onChange={(event) => onSetDpi(clampDpi(Number(event.target.value) || DEFAULT_DPI))}
            aria-label="Rendering DPI"
          />
        </div>
        <span className="ocr-compare-configure-hint muted">
          higher = sharper, slower · default {DEFAULT_DPI}
        </span>
      </div>
      <label className="ocr-compare-configure-field">
        <span className="ocr-compare-configure-label">Language</span>
        <PanelSelect
          className="row-height-select"
          data-testid="ocr-compare-configure-language"
          value={options.language}
          onChange={(event) => onSetLanguage(event.target.value)}
        >
          {MEDIA_LANGUAGE_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </PanelSelect>
      </label>
      <label className="ocr-compare-configure-field">
        <span className="ocr-compare-configure-label">Pages</span>
        <input
          className="form-input"
          data-testid="ocr-compare-configure-pages"
          value={options.pagesText}
          placeholder="1,2,3"
          aria-describedby="ocr-compare-pages-hint"
          aria-invalid={pagesProblem ? true : undefined}
          onChange={(event) => onSetPagesText(event.target.value)}
        />
        <span id="ocr-compare-pages-hint" className="ocr-compare-configure-hint muted">
          comma-separated · up to {MAX_SAMPLE_PAGES} pages · default 1
        </span>
        {pagesProblem ? <span className="form-error" role="alert">{pagesProblem}</span> : null}
      </label>
      <label className="ocr-compare-configure-field">
        <span>
          <input
            type="checkbox"
            data-testid="ocr-compare-configure-searchable-pdf"
            checked={options.searchablePdf}
            onChange={(event) => onSetSearchablePdf(event.target.checked)}
          />{' '}
          Create searchable PDF output
        </span>
      </label>
    </fieldset>
  );
}
