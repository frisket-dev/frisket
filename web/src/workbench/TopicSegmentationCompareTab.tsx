import { CompareToolbar, CompareEngineChip, CompareResultColumn } from './CompareLayout';
import { CompareRunControl } from './CompareRunControl';
import './TopicSegmentationCompareTab.css';
import { Button } from '../components/Button';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
} from 'react';
import {
  Plus,
} from 'lucide-react';

import type { ProjectApiPort } from '../api/ports';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type {
  TopicSegmentationCompareEngineResult,
  TopicSegmentationCompareUnit,
  TopicSegmentationDetail,
} from '../api/open';
import { engineUnavailableReason } from '../actions/engineCatalog';
import { ResizeSeam } from '../components/ResizeSeam';
import { SegmentedToggle } from '../components/PanelPrimitives';
import { CompareBillableEnginesNote, ConfigureVariantPopover } from './MediaCompareShell';
import {
  CompareColumnsState,
  CompareDocList,
  CompareFooter,
  CompareFrontDoor,
  CompareResultState,
  CompareVoteControl,
} from './mediaCompareBodyParts';
import { useCompareVariantConfigure } from './useCompareVariantConfigure';
import {
  PEEK_MAX_WIDTH,
  PEEK_MIN_WIDTH,
  useMediaCompareSession,
  variantPip,
  type CompareColumn,
  type EngineRunState,
  type MediaCompareConfig,
  type ScratchDoc,
} from './mediaCompareSession';
import {
  differingBoundaryKeys,
  formatTopicUnitTime,
} from './topicSegmentationCompareModel';

const TESTID = 'topic-compare';
const TOPIC_COMPARE_ACCEPT = '.txt,.srt,.vtt,text/plain,text/vtt,application/x-subrip';
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
  selectorActionId: 'map.find_topic_sections',
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
    updateColumnOptions,
    addEngineColumn,
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

  const {
    configureVariantId: configureColumnId, configureAnchorRef, configurePopoverRef,
    engineSelectorRef, popoverShift, openConfigure, showConfigure, addVariant,
    onRemoveVariant: removeVariant, onChooseEngine, onDuplicate,
  } = useCompareVariantConfigure(session);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const moreInputRef = useRef<HTMLInputElement>(null);
  const sourceUnitsRef = useRef<HTMLDivElement>(null);
  const [navTarget, setNavTarget] = useState<{ docId: string; key: number } | null>(null);
  const [diffIndex, setDiffIndex] = useState(0);

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
  const noAvailableEngines = runnableColumns.length === 0 && catalog.length > 0 && catalog.every((engine) => engine.available === false);

  return (
    <section className="ocr-compare-tab topic-compare-tab" data-testid="topic-compare-tab">
      {session.selectorOwners}
      <CompareToolbar testIdPrefix="topic-compare" running={runningCount > 0}
        controls={<>
          <CompareRunControl testIdPrefix="topic-compare"
            busy={runState === 'running' || runProgress !== null}
            disabled={!docs.length || runState === 'disabled' || runState === 'probing'}
            progress={runProgress ? `${runProgress.done} / ${runProgress.total}`
              : pendingPairs.length ? `${pendingPairs.length} pending` : undefined}
            statusDetail={!docs.length ? 'Drop a transcript sample first'
              : !runnableColumns.length ? 'Add an available topic-segmentation variant' : undefined}
            onRun={() => pendingPairs.length ? runPending() : runAll()}
            onCancel={session.cancelRuns} />
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
          <Button
            type="button"
            className="ocr-compare-source-toggle"
            data-testid="topic-compare-source-toggle"
            aria-pressed={sourceOpen}
            onClick={() => setSourceOpen((open) => !open)}
          >
            ⊟ Source
          </Button>
        </>}
        note={<CompareBillableEnginesNote
          testidPrefix="topic-compare"
          engines={billableEngines}
          actionLabel="the Find topic sections action"
        />}
      >
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
              <CompareEngineChip testIdPrefix="topic-compare" engine={chipEngine}
                engineId={column.engineId} label={engineLabel(column.engineId)} variantId={column.id}
                pip={pip} summary={summary}
                onBodyClick={() => openConfigure(column.id)}
                onConfigure={() => showConfigure(column.id)}
                onRemove={() => removeVariant(column.id)} />
              {configureColumnId === column.id ? (
                <ConfigureVariantPopover
                  selectorProjection={session.selectorProjection(column)}
                  testidPrefix="topic-compare"
                  actionId="map.find_topic_sections"
                  popoverRef={configurePopoverRef}
                  style={popoverShift ? { transform: `translateX(${popoverShift}px)` } : undefined}
                  column={column}
                  catalog={catalog}
                  engineSelectorRef={engineSelectorRef}
                  errorMessage={run?.status === 'error' ? run.message : null}
                  onChooseEngine={(engineId) => onChooseEngine(column.id, engineId)}
                  onDuplicate={() => onDuplicate(column.id)}
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
          <Plus size={12} /> Variant
        </button>
      </CompareToolbar>

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
            <CompareColumnsState
              testIdPrefix="topic-compare"
              configuredCount={columns.length}
              runnableCount={runnableColumns.length}
              showReady={false}
              noVariantsLabel="Add a variant to inspect topic boundaries."
              unavailableLabel="No variant is ready — configure one to finish setup."
            />
            {runnableColumns.map((column) => {
              const run = activeDoc?.runs[column.id];
              const vote = activeDoc?.votes[column.id] ?? 'neutral';
              return (
                <CompareResultColumn
                  data-testid="topic-compare-engine-column"
                  data-engine-id={column.engineId ?? ''}
                  data-variant-id={column.id}
                  data-run-status={run?.status ?? 'pending'}
                  key={column.id}
                  header={(
                    <>
                      <CompareVoteControl
                        testId="topic-compare-vote-chip"
                        engineId={column.engineId}
                        vote={vote}
                        label={engineLabel(column.engineId)}
                        summary={detailOf(column)}
                        onVote={() => castVote(column.id)}
                      />
                      {run?.status === 'done' ? (
                        <span className="topic-compare-lane-meta mono muted">
                          {run.results.result.sections.length} sections ·{' '}
                          {run.results.result.runtime_ms}ms
                        </span>
                      ) : null}
                    </>
                  )}
                  textClassName="topic-compare-engine-text"
                >
                  <CompareResultState
                    run={run}
                    engineLabel={engineLabel(column.engineId)}
                    pendingLabel="not run yet — select Run comparison"
                    runningLabel="finding topics…"
                    testIdPrefix="topic-compare-engine"
                    onRetry={activeDoc ? () => retryColumn(activeDoc, column) : undefined}
                    renderResult={(completed) => activeParsed ? (
                      <TopicSectionsLane
                        result={completed.results.result}
                        units={completed.results.units}
                        diffKeys={effectiveMode === 'diff' ? diffKeys : []}
                        onNavigate={navigateToBoundary}
                      />
                    ) : null}
                  />
                </CompareResultColumn>
              );
            })}
          </div>
        </div>
      )}

      <CompareFooter
        footerTestId="topic-compare-footer"
        navigationTestId="topic-compare-boundary-nav"
        verdictTestId="topic-compare-verdict"
        verdict={verdict}
        difference={effectiveMode === 'diff' && diffKeys.length > 0 ? {
          label: <>{diffKeys.length} boundary {diffKeys.length === 1 ? 'position' : 'positions'} differ</>,
          previousLabel: 'Previous differing boundary',
          nextLabel: 'Next differing boundary',
          onPrevious: () => jumpBoundary(-1),
          onNext: () => jumpBoundary(1),
        } : null}
        fallback={docs.length === 0
          ? 'drop transcripts to compare'
          : pendingPairs.length > 0
            ? `${pendingPairs.length} pending — select Run comparison`
            : effectiveMode === 'diff'
              ? doneResults
                ? 'no boundary differences'
                : 'waiting for both variants'
              : 'survey — no diff'}
      />
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
