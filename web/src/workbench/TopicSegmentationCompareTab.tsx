import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
} from 'react';
import {
  ChevronLeft,
  ChevronRight,
  Loader2,
  Play,
  Plus,
  RotateCw,
  Settings2,
  X,
} from 'lucide-react';

import type { ProjectApiPort } from '../api/ports';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type {
  TopicSegmentationCompareEngineResult,
  TopicSegmentationCompareUnit,
  TopicSegmentationDetail,
} from '../api/open';
import { engineUnavailableReason, tierForEngine } from '../actions/engineCatalog';
import { EngineTierBadge } from '../components/EngineTierBadge';
import { ResizeSeam } from '../components/ResizeSeam';
import { SegmentedToggle } from '../components/PanelPrimitives';
import { CompareBillableEnginesNote, ConfigureVariantPopover } from './MediaCompareShell';
import { CompareDocList, CompareFrontDoor } from './mediaCompareBodyParts';
import {
  PEEK_MAX_WIDTH,
  PEEK_MIN_WIDTH,
  VARIANT_PIP_GLYPH,
  useMediaCompareSession,
  variantPip,
  type CompareColumn,
  type EngineRunState,
  type MediaCompareConfig,
  type ScratchDoc,
  type Vote,
} from './mediaCompareSession';
import {
  differingBoundaryKeys,
  formatTopicUnitTime,
} from './topicSegmentationCompareModel';

const TESTID = 'topic-compare';
const TOPIC_COMPARE_ACCEPT = '.txt,.srt,.vtt,text/plain,text/vtt,application/x-subrip';
const VOTE_GLYPH: Record<Vote, string> = {
  neutral: '·',
  keep: '✓',
  reject: '✗',
};

interface TopicCompareColumnResult {
  result: TopicSegmentationCompareEngineResult;
  units: TopicSegmentationCompareUnit[];
  sourceKind: 'untimed_transcript' | 'timestamped_transcript';
}

function detailOf(column: CompareColumn): TopicSegmentationDetail {
  const detail = column.options.detail;
  return detail === 'fewer' || detail === 'more' ? detail : 'balanced';
}

function resultError(result: TopicSegmentationCompareEngineResult | undefined): string | null {
  if (!result) return 'Topic-segmentation engine returned no result.';
  if (result.status === 'completed') return null;
  const first = result.errors[0];
  return first?.message ?? first?.code ?? 'Topic-segmentation engine failed.';
}

function firstDoneResult(
  doc: ScratchDoc<TopicCompareColumnResult>,
): TopicCompareColumnResult | null {
  for (const run of Object.values(doc.runs)) {
    if (run.status === 'done') return run.results;
  }
  return null;
}

function createTopicConfig(api: ProjectApiPort): MediaCompareConfig<TopicCompareColumnResult> {
  return {
  testidPrefix: TESTID,
  accept: TOPIC_COMPARE_ACCEPT,
  classifyFile: (file) => (/\.(txt|srt|vtt)$/i.test(file.name) ? 'text' : null),
  acceptHint: 'Topic Compare reads .txt, .srt and .vtt transcripts.',
  defaultEngineIds: [],
  fallbackCatalog: [],
  enginesFromCatalog: (catalog) =>
    catalog?.actions.find((entry) => entry.kind === 'map.find_topic_sections')
      ?.ui_hints.engines ?? [],
  defaultColumnOptions: { detail: 'balanced' },
  docSecondary: (doc) => {
    const parsed = firstDoneResult(doc);
    if (!parsed) return 'transcript · not run yet';
    return `${parsed.units.length} units · ${
      parsed.sourceKind === 'timestamped_transcript' ? 'timed' : 'text'
    }`;
  },
  runColumn: async (doc, column) => {
    const engineId = column.engineId as string;
    const payload = await api.compareTopicSegmentationScratch(doc.file, {
      variants: [
        {
          id: column.id,
          engine: engineId,
          settings: { detail: detailOf(column) },
        },
      ],
    });
    const result = payload.results.find((item) => item.variant_id === column.id);
    const error = resultError(result);
    if (error || !result) return { ok: false, message: error ?? 'Topic segmentation failed.' };
    return {
      ok: true,
      results: {
        result,
        units: payload.units,
        sourceKind: payload.source.source_kind,
      },
      confidence: null,
    };
  },
  // Topic Compare has its own boundary-key diff and section lanes. The shared
  // session still owns documents, variants, delta runs, stale-result guards,
  // votes, and progress; no second compare state machine is needed.
  unitsForDoc: () => [],
  autoRun: false,
  noDiffUnitLabel: 'no boundary differences',
  };
}

interface TopicSegmentationCompareTabProps {
  onSessionChange(state: { hasData: boolean; verdict: string }): void;
}

export function TopicSegmentationCompareTab({
  onSessionChange,
}: TopicSegmentationCompareTabProps) {
  const { projectApi } = useWorkspaceStores();
  const config = useMemo(() => createTopicConfig(projectApi), [projectApi]);
  const session = useMediaCompareSession(config, onSessionChange);
  const {
    catalog,
    billableEngines,
    columns,
    runnableColumns,
    docs,
    activeDoc,
    activeDocId,
    engineLabel,
    effectiveMode,
    setMode,
    pendingPairs,
    runningCount,
    runState,
    runProgress,
    runPending,
    runAll,
    retryColumn,
    removeColumn,
    updateColumnOptions,
    addBlankColumn,
    addEngineColumn,
    duplicateColumn,
    chooseEngine,
    castVote,
    verdict,
    sourceOpen,
    setSourceOpen,
    peekResize,
  } = session;

  const seededColumns = useRef(false);
  useEffect(() => {
    if (seededColumns.current || catalog.length === 0) return;
    seededColumns.current = true;
    const available = catalog
      .filter((engine) => engine.available !== false)
      .sort((left, right) => Number(Boolean(right.recommended)) - Number(Boolean(left.recommended)));
    for (const engine of available.slice(0, 2)) {
      addEngineColumn(engine.id, { detail: 'balanced' });
    }
  }, [addEngineColumn, catalog]);

  const [configureColumnId, setConfigureColumnId] = useState<string | null>(null);
  const [addingColumnId, setAddingColumnId] = useState<string | null>(null);
  const configureAnchorRef = useRef<HTMLDivElement>(null);
  const configurePopoverRef = useRef<HTMLDivElement>(null);
  const engineSelectRef = useRef<HTMLSelectElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const moreInputRef = useRef<HTMLInputElement>(null);
  const sourceUnitsRef = useRef<HTMLDivElement>(null);
  const [navTarget, setNavTarget] = useState<{ docId: string; key: number } | null>(null);
  const [diffIndex, setDiffIndex] = useState(0);

  const removeVariant = useCallback((columnId: string) => {
    removeColumn(columnId);
    setConfigureColumnId((current) => (current === columnId ? null : current));
    setAddingColumnId((current) => (current === columnId ? null : current));
  }, [removeColumn]);

  useEffect(() => {
    if (!configureColumnId) return undefined;
    const dismiss = (event: PointerEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent && event.key !== 'Escape') return;
      if (
        event instanceof PointerEvent &&
        (configurePopoverRef.current?.contains(event.target as Node) ||
          configureAnchorRef.current?.contains(event.target as Node))
      ) {
        return;
      }
      const column = columns.find((candidate) => candidate.id === configureColumnId);
      if (addingColumnId === configureColumnId && !column?.engineId) {
        removeVariant(configureColumnId);
      } else {
        setConfigureColumnId(null);
      }
    };
    window.addEventListener('pointerdown', dismiss);
    window.addEventListener('keydown', dismiss);
    return () => {
      window.removeEventListener('pointerdown', dismiss);
      window.removeEventListener('keydown', dismiss);
    };
  }, [addingColumnId, columns, configureColumnId, removeVariant]);

  const addVariant = useCallback(() => {
    const columnId = addBlankColumn();
    setAddingColumnId(columnId);
    setConfigureColumnId(columnId);
  }, [addBlankColumn]);

  const activeParsed = useMemo(
    () => (activeDoc ? firstDoneResult(activeDoc) : null),
    [activeDoc],
  );
  const doneResults = useMemo(() => {
    if (!activeDoc || runnableColumns.length !== 2) return null;
    const runs = runnableColumns.map((column) => activeDoc.runs[column.id]);
    if (runs.some((run) => run?.status !== 'done')) return null;
    return (runs as Array<Extract<EngineRunState<TopicCompareColumnResult>, { status: 'done' }>>)
      .map((run) => run.results.result);
  }, [activeDoc, runnableColumns]);
  const diffKeys = useMemo(
    () => effectiveMode === 'diff' && doneResults
      ? differingBoundaryKeys(
          doneResults[0].canonical_boundaries,
          doneResults[1].canonical_boundaries,
        )
      : [],
    [doneResults, effectiveMode],
  );
  const navKey = navTarget?.docId === activeDocId ? navTarget.key : null;

  const navigateToBoundary = useCallback((key: number) => {
    if (!activeDocId) return;
    setNavTarget({ docId: activeDocId, key });
    setSourceOpen(true);
  }, [activeDocId, setSourceOpen]);
  const jumpBoundary = useCallback((delta: number) => {
    if (diffKeys.length === 0) return;
    const next = (diffIndex + delta + diffKeys.length) % diffKeys.length;
    setDiffIndex(next);
    navigateToBoundary(diffKeys[next]);
  }, [diffIndex, diffKeys, navigateToBoundary]);
  useEffect(() => {
    if (navKey === null || !sourceOpen) return;
    sourceUnitsRef.current
      ?.querySelector<HTMLElement>(`[data-source-ordinal="${navKey}"]`)
      ?.scrollIntoView({ block: 'center' });
  }, [activeDocId, navKey, sourceOpen]);

  const onDrop = useCallback((event: DragEvent) => {
    event.preventDefault();
    session.ingestFiles(Array.from(event.dataTransfer.files));
  }, [session]);
  const runLabel = docs.length === 0
    ? 'Run'
    : runState === 'running'
      ? `Running · ${runProgress?.done ?? 0} / ${runProgress?.total ?? runningCount}`
      : runState === 'fresh'
        ? 'Re-run'
        : 'Run';
  const noAvailableEngines = catalog.length > 0 && catalog.every((engine) => engine.available === false);

  return (
    <section className="ocr-compare-tab topic-compare-tab" data-testid="topic-compare-tab">
      <div
        className="ocr-compare-toolbar"
        data-testid="topic-compare-toolbar"
        data-running={runningCount > 0 ? 'true' : 'false'}
      >
        <CompareBillableEnginesNote
          testidPrefix="topic-compare"
          engines={billableEngines}
          actionLabel="the Find topic sections action"
        />
        <div className="ocr-compare-controls-row" data-testid="topic-compare-controls-row">
          <button
            type="button"
            className={`btn ocr-compare-run${runState === 'pending' ? ' btn-primary' : ''}`}
            data-testid="topic-compare-run"
            data-run-state={runState}
            data-pending-count={pendingPairs.length}
            disabled={docs.length === 0 || runState === 'disabled' || runState === 'running'}
            title={
              runnableColumns.length === 0
                ? 'Add a topic-segmentation variant'
                : docs.length === 0
                  ? 'Drop a transcript sample first'
                  : runState === 'fresh'
                    ? 'Re-run every document and variant'
                    : undefined
            }
            onClick={() => (runState === 'fresh' ? runAll() : runPending())}
          >
            {runState === 'running' ? (
              <Loader2 size={13} className="ocr-compare-spin" aria-hidden />
            ) : runState === 'fresh' ? (
              <RotateCw size={13} aria-hidden />
            ) : (
              <Play size={13} aria-hidden />
            )}
            <span className="ocr-compare-run-label">{runLabel}</span>
            {runState === 'pending' ? (
              <span className="ocr-compare-run-count mono"> · {pendingPairs.length} pending</span>
            ) : null}
          </button>
          <SegmentedToggle
            testId="topic-compare-mode-switch"
            className="ocr-compare-mode-switch"
            fullWidth={false}
            ariaLabel="Diff or survey view"
            value={effectiveMode}
            onValueChange={(value) => setMode(value as 'diff' | 'survey')}
            buttonTestId={(value) => `topic-compare-mode-${value}`}
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
            data-testid="topic-compare-source-toggle"
            aria-pressed={sourceOpen}
            onClick={() => setSourceOpen((open) => !open)}
          >
            ⊟ Source
          </button>
        </div>

        <div className="ocr-compare-chips-row" data-testid="topic-compare-chips-row">
          <span className="ocr-compare-scratch-tag" data-testid="topic-compare-scratch-tag">
            scratch · not in a project
          </span>
          <span className="ocr-compare-variants-label">VARIANTS</span>
          <div className="ocr-compare-variant-chips" data-testid="topic-compare-variant-chips">
            {columns.map((column) => {
              const run = activeDoc?.runs[column.id];
              const pip = variantPip(column, run);
              const detail = detailOf(column);
              const duplicateEngine =
                columns.filter((candidate) => candidate.engineId === column.engineId).length > 1;
              const summary = detail !== 'balanced' || duplicateEngine ? detail : '';
              const chipEngine = catalog.find((candidate) => candidate.id === column.engineId);
              return (
                <div
                  className="ocr-compare-variant"
                  key={column.id}
                  ref={configureColumnId === column.id ? configureAnchorRef : undefined}
                >
                  <div
                    className="ocr-compare-variant-chip engine-tier-chip"
                    data-testid="topic-compare-variant-chip"
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
                      data-testid="topic-compare-variant-body"
                      onClick={() => setConfigureColumnId((current) => (
                        current === column.id ? null : column.id
                      ))}
                    >
                      <span className="ocr-compare-variant-name">
                        {engineLabel(column.engineId)}
                      </span>
                      {/* Where the chosen engine runs, visible on the chip
                          itself. */}
                      {chipEngine && (
                        <EngineTierBadge
                          tier={tierForEngine(chipEngine)}
                          testId="topic-compare-variant-tier"
                        />
                      )}
                      {summary ? (
                        <span className="ocr-compare-variant-summary muted"> · {summary}</span>
                      ) : null}
                    </button>
                    <button
                      type="button"
                      className="ocr-compare-variant-gear"
                      data-testid="topic-compare-variant-gear"
                      aria-label={`Configure ${engineLabel(column.engineId)}`}
                      onClick={() => setConfigureColumnId(column.id)}
                    >
                      <Settings2 size={12} />
                    </button>
                    <button
                      type="button"
                      className="ocr-compare-variant-remove"
                      data-testid="topic-compare-variant-remove"
                      aria-label={`Remove ${engineLabel(column.engineId)}`}
                      onClick={() => removeVariant(column.id)}
                    >
                      <X size={11} />
                    </button>
                  </div>
                  {configureColumnId === column.id ? (
                    <ConfigureVariantPopover
                      testidPrefix="topic-compare"
                      popoverRef={configurePopoverRef}
                      column={column}
                      catalog={catalog}
                      engineSelectRef={engineSelectRef}
                      errorMessage={run?.status === 'error' ? run.message : null}
                      onChooseEngine={(engineId) => {
                        chooseEngine(column.id, engineId);
                        setAddingColumnId(null);
                      }}
                      onDuplicate={() => {
                        const copyId = duplicateColumn(column.id);
                        if (copyId) setConfigureColumnId(copyId);
                      }}
                      renderOptionFields={() => (
                        <TopicVariantOptions
                          detail={detail}
                          onChange={(nextDetail) => updateColumnOptions(column.id, {
                            detail: nextDetail,
                          })}
                        />
                      )}
                    />
                  ) : null}
                </div>
              );
            })}
            <button
              type="button"
              className="ocr-compare-add-engine engine-tier-chip"
              data-testid="topic-compare-add-variant"
              disabled={catalog.length === 0 || noAvailableEngines}
              onClick={addVariant}
            >
              <Plus size={12} /> variant
            </button>
          </div>
        </div>
      </div>

      {noAvailableEngines ? (
        <div className="topic-compare-catalog-message" role="status">
          No topic-segmentation engine is available in this installation.
          {/* Unavailability honesty: each engine's own catalog reason, not
              a bare "none available". */}
          <ul className="topic-compare-catalog-reasons">
            {catalog.map((engine) => {
              const reason = engineUnavailableReason(engine);
              return reason ? (
                <li key={engine.id} data-testid="topic-compare-catalog-reason">
                  {engine.label}: {reason}
                </li>
              ) : null;
            })}
          </ul>
        </div>
      ) : null}

      {docs.length === 0 ? (
        <CompareFrontDoor
          t={(suffix) => `${TESTID}-${suffix}`}
          session={session}
          config={config}
          fileInputRef={fileInputRef}
          onDropZone={onDrop}
          frontDoorTitle="Drop transcript samples"
          frontDoorHint="TXT, SRT, or VTT · nothing is added to your project"
        />
      ) : (
        <div className="ocr-compare-body" data-testid="topic-compare-body">
          <CompareDocList
            t={(suffix) => `${TESTID}-${suffix}`}
            session={session}
            config={config}
            moreInputRef={moreInputRef}
            onDropZone={onDrop}
          />

          {sourceOpen && activeDoc ? (
            <>
              <div
                className="ocr-compare-source-peek topic-compare-source-peek"
                data-testid="topic-compare-source-peek"
                style={{ width: peekResize.width }}
              >
                <div className="ocr-compare-peek-head">
                  <strong>{activeDoc.filename}</strong>
                  <span className="muted">server-parsed transcript</span>
                </div>
                <div className="topic-compare-source-units" ref={sourceUnitsRef}>
                  {activeParsed ? activeParsed.units.map((unit) => (
                    <TopicSourceUnit key={unit.id} unit={unit} active={unit.ordinal === navKey} />
                  )) : (
                    <div className="ocr-compare-engine-pending muted">
                      Run a variant to inspect the parsed transcript.
                    </div>
                  )}
                </div>
              </div>
              <ResizeSeam
                className={`ocr-compare-peek-resize${peekResize.resizing ? ' resizing' : ''}`}
                testId="topic-compare-peek-resize"
                ariaLabel="Resize the transcript source peek"
                width={peekResize.width}
                min={PEEK_MIN_WIDTH}
                max={PEEK_MAX_WIDTH}
                onResizeStart={peekResize.onResizeStart}
                onResizeKeyDown={peekResize.onResizeKeyDown}
              />
            </>
          ) : null}

          <div
            className="ocr-compare-columns topic-compare-columns"
            data-column-count={runnableColumns.length}
          >
            {runnableColumns.length === 0 ? (
              <div className="ocr-compare-empty-variants muted">
                Add a variant to inspect topic boundaries.
              </div>
            ) : null}
            {runnableColumns.map((column) => {
              const run = activeDoc?.runs[column.id];
              const vote = activeDoc?.votes[column.id] ?? 'neutral';
              return (
                <div
                  className="ocr-compare-engine-column"
                  data-testid="topic-compare-engine-column"
                  data-engine-id={column.engineId ?? ''}
                  data-variant-id={column.id}
                  data-run-status={run?.status ?? 'pending'}
                  key={column.id}
                >
                  <div className="ocr-compare-engine-head">
                    <button
                      type="button"
                      className="ocr-compare-vote-chip"
                      data-testid="topic-compare-vote-chip"
                      data-vote={vote}
                      onClick={() => castVote(column.id)}
                    >
                      <span className="ocr-compare-vote-glyph">{VOTE_GLYPH[vote]}</span>
                      <span className="ocr-compare-vote-label">{engineLabel(column.engineId)}</span>
                      <span className="ocr-compare-col-summary muted mono">
                        {detailOf(column)}
                      </span>
                    </button>
                    {run?.status === 'done' ? (
                      <span className="topic-compare-lane-meta mono muted">
                        {run.results.result.sections.length} sections · {run.results.result.runtime_ms}ms
                      </span>
                    ) : null}
                  </div>
                  <div className="ocr-compare-engine-text topic-compare-engine-text">
                    {!run ? (
                      <div className="ocr-compare-engine-pending muted">
                        not run yet — press Run
                      </div>
                    ) : run.status === 'running' ? (
                      <div className="ocr-compare-engine-running muted">
                        <Loader2 size={13} className="ocr-compare-spin" /> finding topics…
                      </div>
                    ) : run.status === 'error' ? (
                      <div className="ocr-compare-engine-error" role="alert">
                        <strong>{engineLabel(column.engineId)} failed</strong>
                        <span>{run.message}</span>
                        {activeDoc ? (
                          <button
                            type="button"
                            className="btn ocr-compare-engine-retry"
                            data-testid="topic-compare-engine-retry"
                            onClick={() => retryColumn(activeDoc, column)}
                          >
                            <RotateCw size={12} /> Retry
                          </button>
                        ) : null}
                      </div>
                    ) : activeParsed ? (
                      <TopicSectionsLane
                        result={run.results.result}
                        units={run.results.units}
                        diffKeys={effectiveMode === 'diff' ? diffKeys : []}
                        onNavigate={navigateToBoundary}
                      />
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <footer className="ocr-compare-footer" data-testid="topic-compare-footer">
        <div className="ocr-compare-token-nav" data-testid="topic-compare-boundary-nav">
          {effectiveMode === 'diff' && diffKeys.length > 0 ? (
            <>
              <button
                type="button"
                className="icon-btn"
                aria-label="Previous differing boundary"
                onClick={() => jumpBoundary(-1)}
              >
                <ChevronLeft size={13} />
              </button>
              <span className="mono">
                {diffKeys.length} boundary {diffKeys.length === 1 ? 'position' : 'positions'} differ
              </span>
              <button
                type="button"
                className="icon-btn"
                aria-label="Next differing boundary"
                onClick={() => jumpBoundary(1)}
              >
                <ChevronRight size={13} />
              </button>
            </>
          ) : (
            <span className="mono muted">
              {docs.length === 0
                ? 'drop transcripts to compare'
                : pendingPairs.length > 0
                  ? `${pendingPairs.length} pending — press Run`
                  : effectiveMode === 'diff'
                    ? doneResults
                      ? 'no boundary differences'
                      : 'waiting for both variants'
                    : 'survey — no diff'}
            </span>
          )}
        </div>
        <div className="ocr-compare-verdict mono" data-testid="topic-compare-verdict">
          Verdict so far: {verdict}
        </div>
      </footer>
    </section>
  );
}

function TopicVariantOptions({
  detail,
  onChange,
}: {
  detail: TopicSegmentationDetail;
  onChange(detail: TopicSegmentationDetail): void;
}) {
  return (
    <div className="ocr-compare-configure-field">
      <span className="ocr-compare-configure-label">Detail</span>
      <SegmentedToggle
        testId="topic-compare-configure-detail"
        fullWidth={false}
        ariaLabel="Topic section detail"
        value={detail}
        onValueChange={(value) => onChange(value as TopicSegmentationDetail)}
        buttonTestId={(value) => `topic-compare-detail-${value}`}
        options={[
          { value: 'fewer', label: 'Fewer' },
          { value: 'balanced', label: 'Balanced' },
          { value: 'more', label: 'More' },
        ]}
      />
      <span className="ocr-compare-configure-hint muted">
        Fewer makes broader sections; more makes narrower sections.
      </span>
    </div>
  );
}

function TopicSourceUnit({
  unit,
  active,
}: {
  unit: TopicSegmentationCompareUnit;
  active: boolean;
}) {
  const time = formatTopicUnitTime(unit);
  return (
    <div
      className={`topic-compare-source-unit${active ? ' active' : ''}`}
      data-source-ordinal={unit.ordinal}
      data-testid="topic-compare-source-unit"
    >
      <div className="topic-compare-source-unit-meta mono muted">
        <span>#{unit.ordinal + 1}</span>
        {time ? <span>{time}</span> : null}
      </div>
      {unit.speaker ? <strong className="topic-compare-speaker">{unit.speaker}</strong> : null}
      <span>{unit.text}</span>
    </div>
  );
}

function TopicSectionsLane({
  result,
  units,
  diffKeys,
  onNavigate,
}: {
  result: TopicSegmentationCompareEngineResult;
  units: TopicSegmentationCompareUnit[];
  diffKeys: number[];
  onNavigate(key: number): void;
}) {
  const unitById = new Map(units.map((unit) => [unit.id, unit]));
  const ownBoundaries = new Map(result.canonical_boundaries.map((boundary) => [boundary.key, boundary]));
  const diffSet = new Set(diffKeys);
  const firstSectionForUnit = new Map<string, number>();
  for (const section of result.sections) {
    for (const unitId of section.unit_ids) {
      if (!firstSectionForUnit.has(unitId)) firstSectionForUnit.set(unitId, section.index);
    }
  }
  const memberships = new Map(
    result.unit_membership.map((membership) => [membership.unit_id, membership.section_indexes]),
  );

  return result.sections.map((section, sectionIndex) => {
    const precedingBoundary = sectionIndex > 0 ? result.canonical_boundaries[sectionIndex - 1] : null;
    return (
      <div className="topic-compare-section" data-testid="topic-compare-section" key={section.index}>
        {precedingBoundary ? (
          <button
            type="button"
            className={`topic-compare-boundary${diffSet.has(precedingBoundary.key) ? ' differs' : ''}`}
            data-testid="topic-compare-boundary"
            data-boundary-key={precedingBoundary.key}
            onClick={() => onNavigate(precedingBoundary.key)}
          >
            <span />
            {diffSet.has(precedingBoundary.key) ? 'different boundary' : 'topic boundary'}
            <span />
          </button>
        ) : null}
        <div className="topic-compare-section-label mono muted">Section {sectionIndex + 1}</div>
        {section.unit_ids.map((unitId) => {
          const unit = unitById.get(unitId);
          if (!unit) return null;
          const ghost =
            diffSet.has(unit.ordinal) &&
            !ownBoundaries.has(unit.ordinal) &&
            firstSectionForUnit.get(unitId) === section.index;
          const shared = (memberships.get(unitId)?.length ?? 0) > 1;
          return (
            <div key={`${section.index}:${unitId}`}>
              {ghost ? (
                <button
                  type="button"
                  className="topic-compare-boundary differs ghost"
                  data-testid="topic-compare-boundary-ghost"
                  data-boundary-key={unit.ordinal}
                  onClick={() => onNavigate(unit.ordinal)}
                >
                  <span /> other variant splits here <span />
                </button>
              ) : null}
              <div className={`topic-compare-lane-unit${shared ? ' shared' : ''}`}>
                <span className="topic-compare-lane-unit-index mono muted">{unit.ordinal + 1}</span>
                <span>{unit.text}</span>
                {shared ? <span className="topic-compare-shared-label">shared context</span> : null}
              </div>
            </div>
          );
        })}
      </div>
    );
  });
}
