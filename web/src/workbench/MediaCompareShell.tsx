// The compare family's popover / add-menu dismissal (OcrCompareTab's/
// TranscribeCompareTab's Configure popovers, TranscribeCompareTab's
// add-engine menu) is owned by those two files' own hooks
// (useOcrVariantConfigure.tsx / useTranscribeVariantConfigure.tsx):
// the Configure popovers own a small inline Escape+outside effect (they are
// inline-absolute, not a top-layer popover candidate); the add-engine menu
// is a native `popover="manual"` element (useNativePopover).
import { memo, useCallback, useMemo, type DragEvent, type ReactNode } from 'react';
import { ChevronLeft, ChevronRight, Copy, Loader2, RotateCw, X } from 'lucide-react';

import type { EngineOption } from '../api/open';
import type { PreviewSampleResult } from '../api/types';
import { formatDuration, formatUsd } from '../format';
import {
  engineTierLabel,
  engineTierOptions,
  engineUnavailableReason,
  tierForEngine,
} from '../actions/engineCatalog';
import { EngineTierBadge } from '../components/EngineTierBadge';
import { PanelSelect } from '../components/PanelSelect';
import { ResizeSeam } from '../components/ResizeSeam';
import { CompareDocList, CompareFrontDoor } from './mediaCompareBodyParts';
import type { DiffToken } from './ocrDiff';
import {
  PEEK_MAX_WIDTH,
  PEEK_MIN_WIDTH,
  type CompareColumn,
  type EngineRunState,
  type MediaCompareConfig,
  type MediaCompareSession,
  type MediaNavTarget,
  type ScratchDoc,
  type Vote,
} from './mediaCompareSession';

// TranscribeCompareTab gets the same per-variant "Configure variant" options
// popover OcrCompareTab's v2 introduced, REUSING this module's exports to
// avoid a divergent one-off popover. `ConfigureVariantPopover`
// below is the OCR popover extracted verbatim (same DOM/classes/behavior) and
// parameterized: the engine-select/remote-gate/duplicate/footer chrome stays
// generic here; each instance supplies its own option FIELDS (OCR: DPI +
// language; transcribe: language + model size + VAD — see
// TranscribeCompareTab's transcribeFieldsForEngine) via `renderOptionFields`.
// There is no remote-gate arm: billable engines never reach `catalog`, so the
// popover has no billing decision to present. The status-pip glyph (`variantPip`/
// `VARIANT_PIP_GLYPH`) lives in mediaCompareSession.ts, not here —
// react-refresh/only-export-components forbids a component file like this one
// from also exporting plain runtime values (the same split
// WorkspaceStoresProvider.tsx/useWorkspaceStores.ts already use).

export interface ConfigureVariantPopoverProps {
  /** testid namespace ('ocr-compare' | 'transcribe-compare'); the popover's
   *  own testids are `${testidPrefix}-configure*` etc. */
  testidPrefix: string;
  popoverRef: React.RefObject<HTMLDivElement>;
  style?: React.CSSProperties;
  column: CompareColumn;
  catalog: EngineOption[];
  engineSelectRef: React.RefObject<HTMLSelectElement>;
  errorMessage: string | null;
  onChooseEngine(engineId: string): void;
  onDuplicate(): void;
  /** The domain option fields once an engine is chosen (OCR: DPI + language;
   *  transcribe: language/model-size/VAD, per-engine declared). */
  renderOptionFields(column: CompareColumn, engine: EngineOption | undefined): ReactNode;
  /** Trailing "stacks later" hint under the fields (empty = omit). */
  laterHint?: string;
}

/** "Configure variant" — the shared options popover. Anchored under its
 *  chip by the caller (popoverRef + style); this component owns only the
 *  popover's own DOM: engine select, domain fields + duplicate footer. */
export function ConfigureVariantPopover({
  testidPrefix,
  popoverRef,
  style,
  column,
  catalog,
  engineSelectRef,
  errorMessage,
  onChooseEngine,
  onDuplicate,
  renderOptionFields,
  laterHint,
}: ConfigureVariantPopoverProps) {
  const t = (suffix: string) => `${testidPrefix}-${suffix}`;
  const engine = column.engineId
    ? catalog.find((candidate) => candidate.id === column.engineId)
    : undefined;
  return (
    <div
      ref={popoverRef}
      className="ocr-compare-configure"
      data-testid={t('configure')}
      style={style}
    >
      <div className="ocr-compare-configure-title">Configure variant</div>
      {errorMessage ? (
        <div className="ocr-compare-configure-error" data-testid={t('configure-error')} role="alert">
          {errorMessage}
        </div>
      ) : null}
      <label className="ocr-compare-configure-field">
        <span className="ocr-compare-configure-label">
          Engine
          {/* The chosen engine's tier badge — local / sidecar / hosted —
              next to the label (the semantic select trigger can't carry
              per-option badges, so the tier ALSO rides in the grouped custom
              menu and every native option's text). */}
          {engine && (
            <EngineTierBadge tier={tierForEngine(engine)} testId={t('configure-engine-tier')} />
          )}
        </span>
        <PanelSelect
          ref={engineSelectRef}
          className="row-height-select"
          data-testid={t('configure-engine')}
          value={column.engineId ?? ''}
          onChange={(event) => onChooseEngine(event.target.value)}
        >
          <option value="" disabled>
            Choose engine…
          </option>
          {/* Grouped by tier (same three-tier vocabulary as EnginePicker);
              unavailable options carry the catalog's own reason inline —
              never a bare disabled entry. */}
          {engineTierOptions(catalog).map((tier) => (
            <optgroup key={tier.tier} label={engineTierLabel(tier.tier)}>
              {tier.engines.map((candidate) => {
                const reason = engineUnavailableReason(candidate);
                return (
                  <option
                    key={candidate.id}
                    value={candidate.id}
                    disabled={candidate.available === false}
                  >
                    {candidate.label}
                    {reason ? ` (unavailable — ${reason})` : ''}
                  </option>
                );
              })}
            </optgroup>
          ))}
        </PanelSelect>
      </label>

      {column.engineId ? (
        <>
          {renderOptionFields(column, engine)}
          {laterHint ? (
            <div className="ocr-compare-configure-later" aria-hidden>
              {laterHint}
            </div>
          ) : null}
          <div className="ocr-compare-configure-footer">
            <button
              type="button"
              className="btn ocr-compare-duplicate"
              data-testid={t('duplicate')}
              onClick={onDuplicate}
            >
              <Copy size={12} /> Duplicate variant
            </button>
            <span className="muted">edits mark it pending</span>
          </div>
        </>
      ) : (
        <div className="ocr-compare-configure-hint muted">Choose an engine to configure options.</div>
      )}
    </div>
  );
}

/** The one place the compare surface explains where the billable engines went.
 *
 *  Billable -> run; not billable -> preview. An engine that bills real money is
 *  not offered here, because a comparison that spends money at a provider
 *  endpoint already IS a run and belongs on the path that records one (a
 *  receipt, a consent record, a spend line). Rather than silently dropping the
 *  engine from the picker, name it and name the action that runs it — a picker
 *  that quietly omits an engine the catalog advertises reads as a bug.
 *
 *  Renders nothing when the catalog offers no billable engine. */
export function CompareBillableEnginesNote({
  testidPrefix,
  engines,
  actionLabel,
}: {
  testidPrefix: string;
  engines: EngineOption[];
  actionLabel: string;
}) {
  if (engines.length === 0) return null;
  return (
    <div
      className="ocr-compare-configure-later muted"
      data-testid={`${testidPrefix}-billable-note`}
    >
      {engines.map((engine) => engine.label).join(', ')}
      {engines.length === 1 ? ' bills' : ' bill'} per call, so {engines.length === 1 ? 'it is' : 'they are'}{' '}
      not part of the free compare. Run {engines.length === 1 ? 'it' : 'them'} with{' '}
      <strong>{actionLabel}</strong>, which estimates the cost, asks you to confirm
      it, and keeps a receipt.
    </div>
  );
}

// MediaCompareShell body — the shared bake-off UI rendered on top of the
// session engine (mediaCompareSession.ts). Two instances ride it (OCR Compare
// v2 + Transcribe Compare); each supplies a source-peek renderer + config and
// wraps this body with its own toolbar. Class names reuse the established
// `ocr-compare-*` compare visual language (the design's "reuse warm palette/
// chip/list-row/tab components — no new visual language"); only the data-testid
// namespace is parameterized per instance. The vote chip below is a HARD
// EXCLUSION from the StatusChip migration — count/vote-cycle chips are a
// different genus and stays bespoke, not pending a future primitive.

const VOTE_GLYPH: Record<Vote, string> = { neutral: '·', keep: '✓', reject: '✗' };

function confidenceLabel<R>(run: EngineRunState<R> | undefined): string {
  if (run?.status !== 'done' || run.confidence === null) return '—';
  return `${Math.round(run.confidence * 100)}%`;
}

export function ComparisonPreviewDetails({ preview, elapsedMs }: {
  preview?: PreviewSampleResult; elapsedMs?: number;
}) {
  if (!preview) return null;
  const files = preview.kind === 'table' ? preview.rows.flatMap((row) => preview.columns
    .filter((column) => column.columnType === 'file')
    .map((column) => ({ name: column.name, url: row[column.name]?.value }))
    .filter((file): file is { name: string; url: string } =>
      typeof file.url === 'string' && file.url.startsWith('/api/projects/'))) : [];
  return <div className="form-hint" data-testid="comparison-preview-details">
    {elapsedMs !== undefined && <div>Elapsed: {formatDuration(elapsedMs)}</div>}
    {preview.accounting && <div>Provider cost: {preview.accounting.cost_actual === null ? 'unknown'
      : formatUsd(preview.accounting.cost_actual)} · {preview.accounting.model_call_count} model calls</div>}
    {preview.kind === 'table' && preview.warnings.map((warning, index) => <p key={index}>{warning}</p>)}
    {files.map(({ name, url }) => <a key={url} href={url} download>Download {name}</a>)}
  </div>;
}

interface SourcePeekPaneProps<R> {
  testidPrefix: string;
  activeDoc: ScratchDoc<R>;
  nav: MediaNavTarget | null;
  width: number;
  renderSourcePeek(doc: ScratchDoc<R>, nav: MediaNavTarget | null): ReactNode;
}

// MediaCompareBody destructures `session` directly, so it re-renders on
// EVERY vote/nav-token change — and used to call `renderSourcePeek(activeDoc,
// nav)` inline at that re-render (a `no-render-in-render` finding),
// rebuilding the whole source-peek subtree (a PDF/media reader) on every
// vote click even though a vote never changes what's being peeked.
// Extracting a NAMED component and wrapping it in `memo` with a comparator
// over what the peek actually reads (doc identity/objectUrl/filename, nav,
// width, the renderer itself — never `votes`/`runs`) lets React bail out of
// that subtree entirely on vote-only re-renders. `renderSourcePeek` itself
// stays a render PROP by design (the two-tab parameterization contract —
// NOT converted to slots/children); the call inside this component's body
// still matches the linter's
// `no-render-in-render` pattern textually, which is why MediaCompareShell.tsx
// is allowlisted for that one site in doctor.config.ts with this same
// writeup. The resize-handle div is deliberately kept OUTSIDE this memo
// boundary (in MediaCompareBody below): its handlers come from
// `useResizable` un-memoized (a plain DOM element re-rendering with fresh
// handler props every commit is free — no component-identity cost — so
// there is no reason to fold it into the comparator and risk a stale
// closure over `width`).
const SourcePeekPane = memo(function SourcePeekPane<R>({
  testidPrefix,
  activeDoc,
  nav,
  width,
  renderSourcePeek,
}: SourcePeekPaneProps<R>) {
  const t = (suffix: string) => `${testidPrefix}-${suffix}`;
  return (
    <div className="ocr-compare-source-peek" data-testid={t('source-peek')} style={{ width }}>
      {renderSourcePeek(activeDoc, nav)}
    </div>
  );
}, sourcePeekPanePropsEqual) as <R>(props: SourcePeekPaneProps<R>) => ReactNode;

function sourcePeekPanePropsEqual<R>(
  prev: Readonly<SourcePeekPaneProps<R>>,
  next: Readonly<SourcePeekPaneProps<R>>,
): boolean {
  return (
    prev.testidPrefix === next.testidPrefix &&
    prev.activeDoc.id === next.activeDoc.id &&
    prev.activeDoc.objectUrl === next.activeDoc.objectUrl &&
    prev.activeDoc.filename === next.activeDoc.filename &&
    prev.activeDoc.mediaKind === next.activeDoc.mediaKind &&
    prev.nav === next.nav &&
    prev.width === next.width &&
    prev.renderSourcePeek === next.renderSourcePeek
  );
}

export interface MediaCompareBodyProps<R> {
  session: MediaCompareSession<R>;
  config: MediaCompareConfig<R>;
  testidPrefix: string;
  fileInputRef: React.RefObject<HTMLInputElement>;
  moreInputRef: React.RefObject<HTMLInputElement>;
  /** Renders the inner source-peek content (pdf.js reader or native player). */
  renderSourcePeek(doc: ScratchDoc<R>, nav: MediaNavTarget | null): ReactNode;
  /** Per-column options summary chip text (empty = none). */
  columnSummary(column: CompareColumn): string;
  frontDoorTitle: string;
  frontDoorHint: string;
  /** Pending-column empty label ("not run yet — press Run" for staged OCR). */
  pendingLabel: string;
}

/** The shared bake-off body: front-door dropzone OR [doc list | source peek |
 *  columns], plus the footer token-nav + verdict tally. Class names are the
 *  compare visual language (`ocr-compare-*`); testids are per-instance. */
export function MediaCompareBody<R>({
  session,
  config,
  testidPrefix,
  fileInputRef,
  moreInputRef,
  renderSourcePeek,
  columnSummary,
  frontDoorTitle,
  frontDoorHint,
  pendingLabel,
}: MediaCompareBodyProps<R>) {
  const t = (suffix: string) => `${testidPrefix}-${suffix}`;
  const {
    docs,
    activeDoc,
    runnableColumns,
    ingestFiles,
    effectiveMode,
    diffArmed,
    diff,
    pendingPairs,
    runningCount,
    sourceOpen,
    peekResize,
    engineLabel,
    castVote,
    retryColumn,
    jumpToken,
    nav,
    wrapText,
    verdict,
    rejectedDrop,
    dismissRejectedDrop,
  } = session;

  const onDropZone = useCallback(
    (event: DragEvent) => {
      event.preventDefault();
      ingestFiles(Array.from(event.dataTransfer.files));
    },
    [ingestFiles],
  );

  const anyDone = docs.some((doc) =>
    Object.values(doc.runs).some((run) => run.status === 'done'),
  );
  const showRunHint = !config.autoRun && docs.length > 0 && !anyDone && runningCount === 0;

  // A diff needs both columns to have produced text. When a column TERMINATES
  // without any — it failed — the diff will never arrive, and the footer must
  // say so instead of reading "waiting for both" forever (the surviving
  // column's text still renders beside it, and its vote chip still works).
  const failedColumns = activeDoc
    ? runnableColumns.filter((column) => activeDoc.runs[column.id]?.status === 'error')
    : [];
  const allColumnsSettled =
    activeDoc !== null &&
    runnableColumns.every((column) => {
      const status = activeDoc.runs[column.id]?.status;
      return status === 'done' || status === 'error';
    });
  const noDiffReason =
    allColumnsSettled && failedColumns.length > 0
      ? failedColumns.length === runnableColumns.length
        ? 'no diff — every variant failed'
        : `no diff — ${failedColumns.map((column) => engineLabel(column.engineId)).join(' and ')} failed`
      : null;

  // Aligned units for the active doc over the runnable columns — drives both the
  // diff render (per-column left/right token rows) and the plain/survey render.
  const units = useMemo(
    () => (activeDoc ? config.unitsForDoc(activeDoc, runnableColumns) : []),
    [activeDoc, runnableColumns, config],
  );

  return (
    <>
      <CompareDropRejectedNotice t={t} message={rejectedDrop} onDismiss={dismissRejectedDrop} />
      {docs.length === 0 ? (
        <CompareFrontDoor
          t={t}
          session={session}
          config={config}
          fileInputRef={fileInputRef}
          onDropZone={onDropZone}
          frontDoorTitle={frontDoorTitle}
          frontDoorHint={frontDoorHint}
        />
      ) : (
        <div className="ocr-compare-body" data-testid={t('body')} data-wrap={wrapText ? 'on' : 'off'}>
          <CompareDocList
            t={t}
            session={session}
            config={config}
            moreInputRef={moreInputRef}
            onDropZone={onDropZone}
          />

          {sourceOpen && activeDoc && (
            <>
              <SourcePeekPane
                testidPrefix={testidPrefix}
                activeDoc={activeDoc}
                nav={nav}
                width={peekResize.width}
                renderSourcePeek={renderSourcePeek}
              />
              <ResizeSeam
                className={`ocr-compare-peek-resize${peekResize.resizing ? ' resizing' : ''}`}
                testId={t('peek-resize')}
                ariaLabel="Resize the source peek"
                width={peekResize.width}
                min={PEEK_MIN_WIDTH}
                max={PEEK_MAX_WIDTH}
                onResizeStart={peekResize.onResizeStart}
                onResizeKeyDown={peekResize.onResizeKeyDown}
              />
            </>
          )}

          <div
            className="ocr-compare-columns"
            data-column-count={runnableColumns.length}
            data-running={runningCount > 0 ? 'true' : 'false'}
          >
            {showRunHint && (
              <div className="ocr-compare-run-hint" data-testid={t('run-hint')}>
                <span>
                  Nothing has run yet — configure variants, then <strong>Run</strong>. Each variant
                  reads the sample when you run it; results land side by side here.
                </span>
              </div>
            )}
            {runnableColumns.length === 0 ? (
              <div className="ocr-compare-empty-variants muted">
                No variants — add an engine to compare.
              </div>
            ) : null}
            {runnableColumns.map((column, columnIndex) => {
              const vote = activeDoc?.votes[column.id] ?? 'neutral';
              const run = activeDoc?.runs[column.id];
              const summary = columnSummary(column);
              return (
                <div
                  className="ocr-compare-engine-column"
                  data-testid={t('engine-column')}
                  data-engine-id={column.engineId ?? ''}
                  data-variant-id={column.id}
                  data-run-status={run?.status ?? 'pending'}
                  key={column.id}
                >
                  <div className="ocr-compare-engine-head">
                    <button
                      type="button"
                      className="ocr-compare-vote-chip"
                      data-testid={t('vote-chip')}
                      data-engine-id={column.engineId ?? ''}
                      data-vote={vote}
                      title="Vote: click to cycle keep / reject"
                      onClick={() => castVote(column.id)}
                    >
                      <span className="ocr-compare-vote-glyph">{VOTE_GLYPH[vote]}</span>
                      <span className="ocr-compare-vote-label">{engineLabel(column.engineId)}</span>
                      {summary ? (
                        <span className="ocr-compare-col-summary muted mono">{summary}</span>
                      ) : null}
                    </button>
                    <span
                      className="ocr-compare-confidence mono muted"
                      data-testid={t('confidence')}
                      data-engine-id={column.engineId ?? ''}
                    >
                      {confidenceLabel(run)}
                    </span>
                  </div>
                  {run?.status === 'done' && <ComparisonPreviewDetails preview={run.preview} elapsedMs={run.elapsedMs} />}
                  <div className="ocr-compare-engine-text">
                    {!activeDoc ? null : !run ? (
                      <div className="ocr-compare-engine-pending muted" data-testid={t('engine-pending')}>
                        {pendingLabel}
                      </div>
                    ) : run.status === 'running' ? (
                      <div className="ocr-compare-engine-running muted" data-testid={t('engine-running')}>
                        <Loader2 size={13} className="ocr-compare-spin" aria-hidden /> running{' '}
                        {engineLabel(column.engineId)}…
                      </div>
                    ) : run.status === 'error' ? (
                      <div
                        className="ocr-compare-engine-error"
                        data-testid={t('engine-error')}
                        role="alert"
                      >
                        <strong>{engineLabel(column.engineId)} failed</strong>
                        <span>{run.message}</span>
                        <button
                          type="button"
                          className="btn ocr-compare-engine-retry"
                          data-testid={t('engine-retry')}
                          onClick={() => retryColumn(activeDoc, column)}
                        >
                          <RotateCw size={12} /> Retry
                        </button>
                      </div>
                    ) : diff && diffArmed ? (
                      diff.units.map((unit, unitIndex) => {
                        const tokens =
                          columnIndex === 0 ? diff.leftTokens[unitIndex] : diff.rightTokens[unitIndex];
                        if (!tokens) return null;
                        const anyChanged = tokens.some((token) => token.changed);
                        return (
                          <div className="ocr-compare-page" key={unit.key} data-unit={unit.key}>
                            {unit.marker && (
                              <div className="ocr-compare-page-marker mono muted">{unit.marker}</div>
                            )}
                            {anyChanged ? (
                              <p className="ocr-compare-diff-text">
                                {tokens.map((token, index) => (
                                  <DiffTokenView
                                    key={index}
                                    token={token}
                                    testidPrefix={testidPrefix}
                                    onClick={() => session.navigateTo(unit.navTarget)}
                                  />
                                ))}
                              </p>
                            ) : (
                              <p className="ocr-compare-nodiff muted">{config.noDiffUnitLabel}</p>
                            )}
                          </div>
                        );
                      })
                    ) : (
                      units.map((unit) => (
                        <div className="ocr-compare-page" key={unit.key} data-unit={unit.key}>
                          {unit.marker && (
                            <div className="ocr-compare-page-marker mono muted">{unit.marker}</div>
                          )}
                          <p className="ocr-compare-plain-text">
                            {unit.textByColumn[column.id] || 'no text'}
                          </p>
                        </div>
                      ))
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <footer className="ocr-compare-footer" data-testid={t('footer')}>
        <div className="ocr-compare-token-nav" data-testid={t('token-nav')}>
          {diff && diff.changedCount > 0 ? (
            <>
              <button
                type="button"
                className="icon-btn"
                data-testid={t('token-prev')}
                aria-label="Previous diff token"
                onClick={() => jumpToken(-1)}
              >
                <ChevronLeft size={13} />
              </button>
              <span className="mono">
                {diff.changedCount} {diff.changedCount === 1 ? 'token' : 'tokens'} differ
                {diff.allCharLevel ? ' · all character-level' : ''}
              </span>
              <button
                type="button"
                className="icon-btn"
                data-testid={t('token-next')}
                aria-label="Next diff token"
                onClick={() => jumpToken(1)}
              >
                <ChevronRight size={13} />
              </button>
            </>
          ) : (
            <span className="mono muted">
              {docs.length === 0
                ? 'drop media to compare'
                : pendingPairs.length > 0
                  ? `${pendingPairs.length} pending${config.autoRun ? '' : ' — press Run'}`
                  : diffArmed
                    ? effectiveMode === 'diff' && diff === null
                      ? noDiffReason ?? 'waiting for both'
                      : 'no differences'
                    : config.enableDiff === false
                      ? 'results shown independently'
                    : 'survey — no diff'}
            </span>
          )}
        </div>
        <div className="ocr-compare-verdict mono" data-testid={t('verdict')}>
          Verdict so far: {verdict}
        </div>
      </footer>
    </>
  );
}

/** A drop this compare could not read, named rather than silently skipped.
 *
 *  Rendered OUTSIDE CompareFrontDoor, which is itself a <button> (no nested
 *  interactive content), and above both drop targets — the empty front door
 *  and the doc list's "drop more" — since either can take the drop. */
function CompareDropRejectedNotice({
  t,
  message,
  onDismiss,
}: {
  t: (suffix: string) => string;
  message: string | null;
  onDismiss(): void;
}) {
  if (!message) return null;
  return (
    <div className="ocr-compare-drop-rejected" data-testid={t('drop-rejected')} role="alert">
      <span>{message}</span>
      <button
        type="button"
        className="icon-btn"
        data-testid={t('drop-rejected-dismiss')}
        aria-label="Dismiss"
        onClick={onDismiss}
      >
        <X size={13} />
      </button>
    </div>
  );
}

function DiffTokenView({
  token,
  testidPrefix,
  onClick,
}: {
  token: DiffToken;
  testidPrefix: string;
  onClick(): void;
}) {
  if (!token.changed) {
    return <span className="ocr-compare-token">{token.text} </span>;
  }
  return (
    <>
      <button
        type="button"
        className="ocr-compare-diff-token"
        data-testid={`${testidPrefix}-diff-token`}
        onClick={onClick}
      >
        {token.chars.map((span, index) =>
          span.changed ? (
            <mark className="ocr-compare-char-changed" key={index}>
              {span.text}
            </mark>
          ) : (
            <span key={index}>{span.text}</span>
          ),
        )}
      </button>{' '}
    </>
  );
}
