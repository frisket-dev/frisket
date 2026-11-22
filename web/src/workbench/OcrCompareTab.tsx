import { CompareToolbar } from './CompareLayout';
import { CompareRunControl } from './CompareRunControl';
import { Button } from '../components/Button';
import { TextInput } from '../components/FormControls';
import { useEffect, useMemo, useRef, useState } from 'react';

import type {
  ColumnDef,
  OcrComparePreviewBlock,
  OcrComparePreviewPageResult,
  OcrCompareScratchInput,
  PreviewOverlayCell,
  PreviewSampleResult,
  Row,
} from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { OcrCompareTarget } from '../actions/ocrCompare';
import { OCR_ENGINE_FALLBACK, ocrEnginesFromCatalog } from '../actions/engineCatalog';
import { SegmentedToggle } from '../components/PanelPrimitives';
import { PanelSelect } from '../components/PanelSelect';
import { readBlobResource } from '../api/raw/blobText';
import { useLoader } from '../hooks/useLoader';
import { resolveMediaValue, type ResolvedMediaValue } from '../media/resolveMediaValue';
import { documentMediaColumns, documentMediaKind } from './documentMedia';
import { CompareVariantChips, MediaCompareBody } from './MediaCompareShell';
import { useOcrVariantConfigure } from './useOcrVariantConfigure';
import { usePaidMediaComparison } from './usePaidMediaComparison';
import {
  MEDIA_LANGUAGE_OPTIONS,
  useMediaCompareSession,
  type AlignedUnit,
  type CompareColumn,
  type MediaCompareConfig,
  type MediaNavTarget,
  type ScratchDoc,
} from './mediaCompareSession';


// Upload-first OCR comparison: each column is an engine/options variant over
// disposable dropped media. Adding or editing stages work until Run.

const DEFAULT_ENGINE_IDS = ['rapidocr', 'dots.mocr'];
const DEFAULT_SEED_MAX_ITEMS = 10;
const MAX_SAMPLE_PAGES = 10;
const DEFAULT_DPI = 200;
const DPI_PRESETS = [100, 200, 300];
const DPI_MIN = 50;
const DPI_MAX = 600;
const clampDpi = (dpi: number) => Math.max(DPI_MIN, Math.min(DPI_MAX, Math.round(dpi)));

const OCR_MIME_EXTENSIONS: Readonly<Record<string, string>> = {
  'application/pdf': '.pdf',
  'image/png': '.png',
  'image/jpeg': '.jpg',
  'image/webp': '.webp',
  'image/tiff': '.tiff',
  'image/bmp': '.bmp',
};

function classifyOcrFile(file: File): 'pdf' | 'image' | null {
  if (file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')) return 'pdf';
  if (file.type.startsWith('image/') || /\.(png|jpe?g|webp|tiff?|bmp)$/i.test(file.name)) return 'image';
  return null;
}

function findOcrSeedColumn(columns: readonly ColumnDef[], sourceColumn?: string): ColumnDef | null {
  if (sourceColumn) {
    const matched = columns.find(
      (column) => column.name === sourceColumn || column.id === sourceColumn,
    );
    if (matched) return matched;
  }
  const mediaColumns = documentMediaColumns({ columns });
  return mediaColumns.find((column) => column.type === 'file' || column.type === 'image')
    ?? mediaColumns[0]
    ?? null;
}

function isPotentialOcrMedia(media: ResolvedMediaValue, columnType: string): boolean {
  const kind = documentMediaKind(media, columnType);
  if (kind === 'pdf' || kind === 'image') return true;
  return kind === 'other' && !media.mime && (columnType === 'file' || columnType === 'image');
}

function ocrFilenameForMedia(media: ResolvedMediaValue, mime: string, index: number): string {
  const baseName = media.filename || media.label || `document-${index + 1}`;
  if (classifyOcrFile(new File([], baseName, { type: '' })) !== null) return baseName;
  const extension = OCR_MIME_EXTENSIONS[mime.toLowerCase()];
  return extension ? `${baseName}${extension}` : baseName;
}

function collectOcrSeedCandidates(
  rows: readonly Row[],
  column: ColumnDef,
  projectId: string,
  maxItems: number,
): ResolvedMediaValue[] {
  const candidates: ResolvedMediaValue[] = [];
  for (const row of rows) {
    if (candidates.length >= maxItems) break;
    const media = resolveMediaValue(row.cells[column.id] ?? null, projectId);
    if (!media || !isPotentialOcrMedia(media, column.type)) continue;
    candidates.push(media);
  }
  return candidates;
}

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
  selectorActionId: 'media.ocr',
  testidPrefix: 'ocr-compare',
  accept: 'application/pdf,image/*,.pdf,.png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp',
  classifyFile: classifyOcrFile,
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
  const { projectApi, chromePreferences: { projectId } } = useWorkspaceStores();
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
    columns,
    docs,
    runnableColumns,
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
  const estimateInputKey = useMemo(() => JSON.stringify({
    docs: docs.map((doc) => doc.id),
    columns: columns.map(({ id, engineId, options }) => ({ id, engineId, options })),
  }), [columns, docs]);

  useEffect(() => {
    if (!active) cancelRuns();
    return () => cancelRuns();
  }, [active, cancelRuns]);

  const { clearEstimate } = paid;
  useEffect(() => { clearEstimate(); }, [clearEstimate, estimateInputKey]);

  const ingestFilesRef = useRef(session.ingestFiles);
  useEffect(() => {
    ingestFilesRef.current = session.ingestFiles;
  }, [session.ingestFiles]);
  const ingestedLaunchRef = useRef<string | null>(null);
  const [seedingDetail, setSeedingDetail] = useState<{ launchKey: string; message: string } | null>(null);

  const targetSheetId = target?.sheetId;
  const targetSourceColumn = target?.sourceColumn;
  const targetMaxItems = Math.max(1, target?.maxItems ?? DEFAULT_SEED_MAX_ITEMS);
  const targetLaunchId = target?.launchId;
  const selectedRowIdsKey = target?.selectedRowIds?.join(',') ?? '';
  const seedLaunchKey = targetSheetId
    ? targetLaunchId != null
      ? `launch:${targetLaunchId}`
      : JSON.stringify([targetSheetId, targetSourceColumn ?? null, selectedRowIdsKey, targetMaxItems])
    : null;

  const seedLoader = useLoader(
    async (signal) => {
      if (!targetSheetId || !seedLaunchKey) return { launchKey: '', files: [] as File[] };
      const selectedIds = selectedRowIdsKey ? selectedRowIdsKey.split(',') : [];
      const fallbackLimit = Math.max(targetMaxItems * 5, 50);
      let candidates: ResolvedMediaValue[] = [];

      if (selectedIds.length > 0) {
        const requestedIds = selectedIds.slice(0, 100);
        const requestedNumericIds = requestedIds
          .map((id) => Number(id))
          .filter((id) => Number.isFinite(id));
        const selectedPage = requestedNumericIds.length > 0
          ? await projectApi.getSheetData(targetSheetId, 0, requestedNumericIds.length, {
              rowIds: requestedNumericIds,
            })
          : await projectApi.getSheetData(targetSheetId, 0, fallbackLimit);
        if (signal.aborted) return { launchKey: seedLaunchKey, files: [] as File[] };
        const targetColumn = findOcrSeedColumn(selectedPage.columns, targetSourceColumn);
        if (targetColumn) {
          const rowById = new Map(selectedPage.rows.map((row) => [String(row.id), row]));
          const orderedRows = requestedNumericIds.length > 0
            ? requestedIds
                .map((id) => rowById.get(id))
                .filter((row): row is Row => row !== undefined)
            : selectedPage.rows;
          candidates = collectOcrSeedCandidates(orderedRows, targetColumn, projectId, targetMaxItems);
          if (candidates.length === 0 && requestedNumericIds.length > 0) {
            const fallbackPage = await projectApi.getSheetData(targetSheetId, 0, fallbackLimit, {
              columnIds: [targetColumn.id],
            });
            if (signal.aborted) return { launchKey: seedLaunchKey, files: [] as File[] };
            candidates = collectOcrSeedCandidates(fallbackPage.rows, targetColumn, projectId, targetMaxItems);
          }
        }
      } else {
        const page = await projectApi.getSheetData(targetSheetId, 0, fallbackLimit);
        if (signal.aborted) return { launchKey: seedLaunchKey, files: [] as File[] };
        const targetColumn = findOcrSeedColumn(page.columns, targetSourceColumn);
        if (targetColumn) {
          candidates = collectOcrSeedCandidates(page.rows, targetColumn, projectId, targetMaxItems);
        }
      }

      if (signal.aborted || candidates.length === 0) {
        return { launchKey: seedLaunchKey, files: [] as File[] };
      }
      setSeedingDetail({
        launchKey: seedLaunchKey,
        message: `Loading ${candidates.length} ${candidates.length === 1 ? 'file' : 'files'} from sheet…`,
      });
      const loaded = await Promise.all(
        candidates.map(async (media, index) => {
          try {
            const blob = await readBlobResource(media.url, { signal });
            if (signal.aborted) return null;
            const mime = media.mime || blob.type || '';
            const filename = ocrFilenameForMedia(media, mime, index);
            const file = new File([blob], filename, { type: mime });
            return classifyOcrFile(file) ? file : null;
          } catch {
            return null;
          }
        }),
      );
      return {
        launchKey: seedLaunchKey,
        files: loaded.filter((file): file is File => file !== null),
      };
    },
    [projectApi, projectId, seedLaunchKey],
    { enabled: Boolean(targetSheetId && seedLaunchKey) },
  );

  useEffect(() => {
    const result = seedLoader.data;
    if (!result || !result.launchKey || ingestedLaunchRef.current === result.launchKey) return;
    ingestedLaunchRef.current = result.launchKey;
    if (result.files.length > 0) ingestFilesRef.current(result.files);
  }, [seedLoader.data]);

  const seedingMessage = seedLoader.loading && seedLaunchKey
    ? (seedingDetail?.launchKey === seedLaunchKey ? seedingDetail.message : 'Loading files from sheet…')
    : null;

  const variantEditor = useOcrVariantConfigure(session);


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

  return (
    <section className="ocr-compare-tab" data-testid="ocr-compare-tab">
      {session.selectorOwners}
      <CompareToolbar testIdPrefix="ocr-compare" running={runningCount > 0}
        controls={<>
          <CompareRunControl testIdPrefix="ocr-compare" busy={busy}
            disabled={!active || !docs.length || runState === 'disabled' || runState === 'probing'}
            workingLabel={preparing ? 'Preparing…' : 'Running…'}
            progress={runProgress ? `${runProgress.done} / ${runProgress.total}`
              : runState === 'probing' ? 'Reading pages…'
                : !preparing && pendingPairs.length ? `${pendingPairs.length} pending` : undefined}
            statusDetail={runState === 'disabled' ? 'Add an available engine and a sample'
              : runState === 'probing' ? 'Reading the page count' : undefined}
            onRun={() => pendingPairs.length ? runPending() : runAll()}
            onCancel={cancelRuns} />
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
          <Button
            type="button"
            className="ocr-compare-source-toggle"
            data-testid="ocr-compare-source-toggle"
            aria-pressed={sourceOpen}
            onClick={() => setSourceOpen((open) => !open)}
          >
            ⊟ Source
          </Button>
          <Button
            type="button"
            className="ocr-compare-wrap-toggle"
            data-testid="ocr-compare-wrap-toggle"
            aria-pressed={wrapText}
            title={wrapText ? 'Wrap text to the panel (on)' : 'No wrap — scroll horizontally'}
            onClick={() => setWrapText((wrap) => !wrap)}
          >
            ⏎ Wrap
          </Button>
        </>}
      >
        <CompareVariantChips
          session={session}
          editor={variantEditor}
          testidPrefix="ocr-compare"
          actionId="media.ocr"
          summary={variantSummary}
          disabled={busy}
          addLabel="Engine"
          addDisabled={busy}
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
        />
      </CompareToolbar>

      <MediaCompareBody
        session={session}
        config={config}
        testidPrefix="ocr-compare"
        renderSourcePeek={variantEditor.renderSourcePeek}
        columnSummary={variantSummary}
        frontDoorTitle="Drop a small sample of PDFs or images"
        frontDoorHint="Nothing uploads to your project — this session is disposable. Configure variants, then Run."
        pendingLabel="not run yet — select Run comparison"
        loadingMessage={seedingMessage}
      />
      {paid.estimateLine}
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
    <fieldset className="ocr-compare-configure-fields" disabled={disabled}>
      <div className="ocr-compare-configure-field">
        <span className="ocr-compare-configure-label">Rendering DPI</span>
        <SegmentedToggle
          testId="ocr-compare-dpi-presets"
          fullWidth={false}
          size="compact"
          ariaLabel="Rendering DPI presets"
          value={String(options.dpi)}
          onValueChange={(value) => onSetDpi(Number(value))}
          buttonTestId={(value) => `ocr-compare-dpi-pill-${value}`}
          options={DPI_PRESETS.map((dpi) => ({ value: String(dpi), label: String(dpi) }))}
        />
        <TextInput
          type="number"
          density="compact"
          fullWidth={false}
          className="ocr-compare-dpi-stepper"
          data-testid="ocr-compare-dpi-stepper"
          min={DPI_MIN}
          max={DPI_MAX}
          step={1}
          value={options.dpi}
          onChange={(event) => onSetDpi(clampDpi(Number(event.target.value) || DEFAULT_DPI))}
          aria-label="Rendering DPI"
        />
        <span className="ocr-compare-configure-hint muted">
          higher = sharper, slower · default {DEFAULT_DPI}
        </span>
      </div>
      <label className="ocr-compare-configure-field form-field">
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
      <label className="ocr-compare-configure-field form-field">
        <span className="ocr-compare-configure-label">Pages</span>
        <TextInput
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
        {pagesProblem ? <span className="panel-error" role="alert">{pagesProblem}</span> : null}
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
