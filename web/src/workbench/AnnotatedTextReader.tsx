// Read one long text cell with the pipeline's entities marked in place, toggle
// each family off, and click a mark.
//
// The sibling of DocumentReader (media) inside the Document view, not a
// replacement for it — the Document view's source is now media-or-text, and
// this renders the text arm. It is also NOT a tweak to EvidenceViewer's
// `highlightQuotes`: that one re-locates each quote by first occurrence in the
// fetched text and merges everything into one highlight, which cannot answer
// "which mention did I click" and puts the mark on the wrong "Ada" whenever a
// name appears twice. This draws at the offsets the server returned, over the
// exact string the server returned.
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from 'react';
import { AlertTriangle, FileSearch, Settings2 } from 'lucide-react';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { TextAnnotations } from '../api/types';
import { PanelHeader } from '../components/PanelPrimitives';
import {
  annotationToggles,
  collectMarks,
  markAriaLabel,
  partitionAnnotationFragments,
  unpositionedExplanation,
  type AnnotationMark,
  type AnnotationToggle,
} from './textAnnotationModel';
import { useOptionsPopover } from './useOptionsPopover';

/** Stable identity so the `layers` memo does not churn on every render for a
 *  cell that has none. */
const EMPTY_LAYERS: TextAnnotations['layers'] = [];

type OptionsPopoverRenderer = (props: {
  popoverRef: (el: HTMLDivElement | null) => void;
  style: CSSProperties;
}) => ReactNode;

/** See DocumentReader's OptionsPopoverSlot: invoking the render-prop from a
 *  component that does not itself own the ref keeps react-hooks/refs' render-
 *  time-ref-access check satisfied. */
function OptionsPopoverSlot({
  popoverRef,
  style,
  render,
}: {
  popoverRef: (el: HTMLDivElement | null) => void;
  style: CSSProperties;
  render: OptionsPopoverRenderer;
}) {
  return <>{render({ popoverRef, style })}</>;
}

interface AnnotatedTextReaderProps {
  /** The cell being read. The reader fetches ONLY this cell's text +
   *  annotations — the sheet-data page that drives the document list carries no
   *  annotations and does not need to. */
  rowId: string | null;
  columnId: string;
  title: string;
  /** Toggle keys turned off for this sheet, from persisted chrome state. */
  disabledToggleKeys: readonly string[];
  onSetDisabledToggleKeys(next: string[]): void;
  /** Click a mark. The mention-detail panel is the destination; a host may
   *  omit the callback to keep marks read-only. */
  onOpenMention?: ((mark: AnnotationMark) => void) | null;
  /** The mention currently open in the detail panel, so its marks read as
   *  selected. */
  activeOccurrenceId?: string | null;
  onOpenDetail(): void;
  canOpenDetail: boolean;
  optionsOpen: boolean;
  onToggleOptions(): void;
  optionsPopover: OptionsPopoverRenderer | null;
  /** Menus that options inside `optionsPopover` portal to <body>; see
   *  DocumentReader's identical prop. */
  optionsMenuRefs?: ReadonlyArray<RefObject<HTMLElement | null>>;
  selectionCount: number;
  /** Re-run the producer for a stale layer, from its OUTPUT column — the
   *  starting point of the stored-action replay. A host that cannot
   *  launch runs passes nothing, and the `[!]` explains without offering a
   *  button it cannot honor. */
  onReplayLayer?: ((outputColumnId: string) => void) | null;
}

/** A SETTLED fetch, keyed by the cell it settled for. Kept keyed (rather than
 *  reset by an effect) so a document switch reads as "loading" during the same
 *  render that changed the key — resetting it from inside the effect would show
 *  the previous document's marks over the new document's title for one frame,
 *  and is a cascading render besides. */
interface LoadState {
  key: string;
  data: TextAnnotations | null;
  error: string | null;
}

export function AnnotatedTextReader({
  rowId,
  columnId,
  title,
  disabledToggleKeys,
  onSetDisabledToggleKeys,
  onOpenMention,
  activeOccurrenceId,
  onOpenDetail,
  canOpenDetail,
  optionsOpen,
  onToggleOptions,
  optionsPopover,
  optionsMenuRefs,
  selectionCount,
  onReplayLayer,
}: AnnotatedTextReaderProps) {
  const { projectApi: api } = useWorkspaceStores();
  const {
    triggerRef: optionsTriggerRef,
    attachPopover: attachOptionsPopover,
    popoverStyle: optionsPopoverStyle,
  } = useOptionsPopover(
    optionsOpen,
    onToggleOptions,
    '[data-testid="text-options-button"]',
    optionsMenuRefs,
  );

  const loadKey = `${rowId ?? ''}:${columnId}`;
  const [settled, setSettled] = useState<LoadState | null>(null);
  const [openStatusKey, setOpenStatusKey] = useState<string | null>(null);

  useEffect(() => {
    if (rowId === null) return;
    let cancelled = false;
    void api
      .getTextAnnotations(rowId, columnId)
      .then((data) => {
        if (cancelled) return;
        setSettled({ key: loadKey, data, error: null });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setSettled({
          key: loadKey,
          data: null,
          error: err instanceof Error ? err.message : 'Could not load this document.',
        });
      });
    return () => {
      cancelled = true;
    };
  }, [loadKey, rowId, columnId]);

  // Opening a result in the mention-detail panel must land ON the occurrence,
  // not at the top of a 90-minute transcript. Keyed on the settled fetch too,
  // so switching documents scrolls once the new text is actually rendered.
  const bodyRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (activeOccurrenceId == null) return;
    const body = bodyRef.current;
    if (body === null) return;
    const target = body.querySelector(
      `[data-occurrence-id="${CSS.escape(activeOccurrenceId)}"][data-owner-first="true"]`,
    );
    // Absent is the ordinary case, not a failure: the mention can be in a
    // layer the reader has toggled off, or in a document the panel listed but
    // whose layer no longer positions.
    if (target instanceof HTMLElement) {
      target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }, [activeOccurrenceId, settled]);

  const current = settled !== null && settled.key === loadKey ? settled : null;
  const loading = rowId !== null && current === null;
  const error = current?.error ?? null;
  const data = current?.data ?? null;
  const layers = useMemo(() => data?.layers ?? EMPTY_LAYERS, [data]);
  const toggles = useMemo(
    () => annotationToggles(layers, disabledToggleKeys),
    [layers, disabledToggleKeys],
  );
  const fragments = useMemo(
    () =>
      partitionAnnotationFragments(data?.text ?? '', collectMarks(layers, disabledToggleKeys)),
    [data?.text, layers, disabledToggleKeys],
  );
  // More than one toggle of the same family means the column name is the only
  // thing telling them apart; with one, the family name alone is cleaner.
  const familyCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const toggle of toggles) {
      counts.set(toggle.layerFamily, (counts.get(toggle.layerFamily) ?? 0) + 1);
    }
    return counts;
  }, [toggles]);

  const toggleLayer = useCallback(
    (toggleKey: string) => {
      const disabled = new Set(disabledToggleKeys);
      if (disabled.has(toggleKey)) disabled.delete(toggleKey);
      else disabled.add(toggleKey);
      onSetDisabledToggleKeys([...disabled]);
    },
    [disabledToggleKeys, onSetDisabledToggleKeys],
  );

  const headerChips = useMemo(
    () => (
      <span className="document-reader-chip pill-btn" data-testid="document-reader-chip">
        Text
      </span>
    ),
    [],
  );

  return (
    <section className="document-reader" data-testid="annotated-text-reader" data-media-kind="text-cell">
      <PanelHeader
        className="panel-frame-header document-reader-header"
        testId="document-reader-header"
        title={
          <span className="document-reader-title" data-testid="document-reader-title" title={title}>
            {title}
          </span>
        }
        chips={headerChips}
        actions={
          <>
            <span
              className="document-reader-selection mono muted"
              data-testid="document-selection"
              data-selection-count={selectionCount}
            >
              {selectionCount > 0 ? `${selectionCount} selected` : 'browsing'}
            </span>
            <span className="document-reader-spacer" />
            <button
              type="button"
              className="icon-btn"
              data-testid="document-open-detail"
              aria-label="Open row detail"
              title="Open row detail"
              disabled={!canOpenDetail}
              onClick={onOpenDetail}
            >
              <FileSearch size={15} />
            </button>
            <div className="document-options-anchor">
              <button
                type="button"
                ref={optionsTriggerRef}
                className={`icon-btn${optionsOpen ? ' active' : ''}`}
                data-testid="text-options-button"
                aria-label="View options"
                aria-expanded={optionsOpen}
                title="View options"
                onClick={onToggleOptions}
              >
                <Settings2 size={15} />
              </button>
              {optionsOpen && optionsPopover && (
                <OptionsPopoverSlot
                  popoverRef={attachOptionsPopover}
                  style={optionsPopoverStyle}
                  render={optionsPopover}
                />
              )}
            </div>
          </>
        }
      />
      {toggles.length > 0 && (
        // A real <fieldset>, not role="group": the group of layer switches has
        // an actual HTML element, and its <legend> is the accessible name a
        // bare aria-label was standing in for.
        <fieldset className="annotation-toggles" data-testid="annotation-toggles">
          <legend className="sr-only-select">Annotation layers</legend>
          {toggles.map((toggle) => (
            <LayerToggleChip
              key={toggle.toggleKey}
              toggle={toggle}
              showColumn={(familyCounts.get(toggle.layerFamily) ?? 0) > 1}
              statusOpen={openStatusKey === toggle.toggleKey}
              onToggle={() => toggleLayer(toggle.toggleKey)}
              onToggleStatus={() =>
                setOpenStatusKey((open) => (open === toggle.toggleKey ? null : toggle.toggleKey))
              }
              onReplay={onReplayLayer ? () => onReplayLayer(toggle.outputColumnId) : null}
            />
          ))}
        </fieldset>
      )}
      <div className="document-reader-body" data-testid="document-reader-body">
        {error !== null && (
          <div className="document-pdf-error" role="alert" data-testid="annotated-text-error">
            {error}
          </div>
        )}
        {loading && (
          <p className="document-textfile muted" data-testid="annotated-text-loading">
            Loading…
          </p>
        )}
        {!loading && error === null && data?.text == null && (
          <div className="document-empty" data-testid="annotated-text-empty">
            <p className="document-empty-title">Nothing to read</p>
            <p className="muted">This row has no text in the source column.</p>
          </div>
        )}
        {data?.text != null && (
          <div className="document-textfile" data-testid="annotated-text-body" ref={bodyRef}>
            <p className="document-text" data-testid="annotated-text-content">
              {fragments.map((fragment) => {
                const owner = fragment.owner;
                if (owner === null) return <span key={fragment.start}>{fragment.text}</span>;
                const marked = {
                  className: 'annotation-mark',
                  'data-testid': 'annotation-mark',
                  'data-occurrence-id': owner.occurrenceId,
                  'data-entity-type': owner.entityType ?? 'unknown',
                  'data-depth': fragment.marks.length,
                  'data-active':
                    activeOccurrenceId === owner.occurrenceId ? 'true' : 'false',
                  // Only the first fragment of an occurrence carries the name and
                  // the tab stop, so a mark split by a nested mark is one stop and
                  // one announcement, not three (R18).
                  'data-owner-first': fragment.ownerFirstFragment ? 'true' : 'false',
                  'aria-label': markAriaLabel(fragment),
                };
                // A clickable mark is a real <button>: focus order, Enter/Space,
                // and the button role come from the element instead of from a
                // hand-rolled keydown handler on a non-interactive tag (R18).
                return onOpenMention ? (
                  <button
                    key={fragment.start}
                    type="button"
                    {...marked}
                    tabIndex={fragment.ownerFirstFragment ? 0 : -1}
                    onClick={() => onOpenMention(owner)}
                  >
                    {fragment.text}
                  </button>
                ) : (
                  <mark key={fragment.start} {...marked}>
                    {fragment.text}
                  </mark>
                );
              })}
            </p>
          </div>
        )}
      </div>
    </section>
  );
}

/** One family's on/off chip. A stale layer keeps its chip and its last-known
 *  count, draws no marks, and grows a `[!]` — degrading to "here is what I
 *  knew, and why I can't show it" rather than to silence. */
function LayerToggleChip({
  toggle,
  showColumn,
  statusOpen,
  onToggle,
  onToggleStatus,
  onReplay,
}: {
  toggle: AnnotationToggle;
  showColumn: boolean;
  statusOpen: boolean;
  onToggle(): void;
  onToggleStatus(): void;
  onReplay: (() => void) | null;
}) {
  const label = showColumn && toggle.outputColumnName
    ? `${toggle.label} · ${toggle.outputColumnName}`
    : toggle.label;
  return (
    <span className="annotation-toggle" data-testid={`annotation-toggle-${toggle.toggleKey}`}>
      <button
        type="button"
        className={`pill-btn annotation-toggle-btn${toggle.enabled ? ' is-active' : ''}`}
        aria-pressed={toggle.enabled}
        // Disabled-looking but still clickable: turning a stale layer back on
        // is how you get its count back, even though it draws nothing.
        data-positioned={toggle.positioned ? 'true' : 'false'}
        onClick={onToggle}
      >
        <span className="annotation-toggle-label">{label}</span>
        <span className="facet-count">{toggle.count.toLocaleString()}</span>
      </button>
      {!toggle.positioned && (
        <button
          type="button"
          className={`icon-btn annotation-toggle-status${statusOpen ? ' active' : ''}`}
          data-testid={`annotation-status-${toggle.toggleKey}`}
          aria-label={`Why ${label} has no highlights`}
          aria-expanded={statusOpen}
          onClick={onToggleStatus}
        >
          <AlertTriangle size={12} />
        </button>
      )}
      {statusOpen && !toggle.positioned && toggle.unpositionedReason !== null && (
        // <output> IS role="status" in HTML, so the live region comes from the
        // element rather than from a role bolted onto a div.
        <output className="annotation-status-note" data-testid="annotation-status-note">
          <p>{unpositionedExplanation(toggle.unpositionedReason)}</p>
          {onReplay !== null ? (
            <>
              {/* Ruling 4 honesty: this door cannot resume — map.ner is a
                  whole-column contract (see nerReplayModel.ts), so the re-run
                  rebuilds EVERY row, not just this stale document. Naming the
                  scope was not enough: a user reasonably reads "re-run" as a
                  repair of the one stale document and expects to pay for one
                  document. Say it is a fresh purchase of the whole column
                  before the click; the cost gate then prices it. */}
              <p className="muted">
                Re-running rebuilds the whole column — every row in the sheet
                runs again, not just this document. That makes it a new run,
                not a resume: the whole column is charged again, including
                rows that are already correct.
              </p>
              <button
                type="button"
                className="mini-btn"
                data-testid={`annotation-replay-${toggle.toggleKey}`}
                onClick={onReplay}
              >
                Re-run NER for this column
              </button>
            </>
          ) : (
            <p className="muted">
              Re-run the extraction for this column to line the highlights up again.
            </p>
          )}
        </output>
      )}
    </span>
  );
}
