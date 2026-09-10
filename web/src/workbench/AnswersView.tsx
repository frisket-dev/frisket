import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
} from 'react';
import { Search } from 'lucide-react';
import type { CellEvidenceLinkSummary, ColumnDef, Row, SheetMeta } from '../api/types';
import type { EvidenceLinkViewerApiPort } from '../api/ports';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { ResizeSeam } from '../components/ResizeSeam';
import { CitationChip } from '../components/evidence/CitationChip';
import { PanelLoading } from '../components/PanelPrimitives';
import { PanelSelect } from '../components/PanelSelect';
import { useResizable } from '../components/useResizable';
import { MarkdownView } from '../markdown';
import { mergeCitedQuote } from './citationQuote';
import { useColumnEvidence } from './columnEvidenceResource';
import { useEvidenceLinkViewer } from './evidenceLinkViewerResource';
import { rowTitle as resolveRowTitle } from './rowTitle';
import { LIST_ITEM_HEIGHT, useWindowedRowList } from './useWindowedRowList';

// Lazy, matching App.tsx's LazyEvidenceViewer -- AnswersView itself is
// imported eagerly (like DocumentView), so a static EvidenceViewer import
// here would pull it into the main bundle and defeat that split (caught by
// the build's INEFFECTIVE_DYNAMIC_IMPORT warning).
const LazyEvidenceViewer = lazy(() =>
  import('../components/EvidenceViewer').then(({ EvidenceViewer }) => ({
    default: EvidenceViewer,
  })),
);

// Three panes: left = rows (rowTitle, selectionStore-synced), middle = the
// chosen cited column's SELECTED row's extraction + citation chips, right =
// the DOCKED EvidenceViewer (mode="pane") driven by answersView.activeLinkId
// — a LOCAL pane, never the global evidence host.
//
// Left-pane virtualization is a direct port of DocumentView.tsx's list
// mechanics (search/scroll math/keyboard nav) so the two views agree.
//
// The middle pane renders ONLY the left-rail's active row — its
// chosen-column value + citation chip(s) — and swaps wholesale on selection
// change. It intentionally does NOT stack every cited column's value for
// that row: `useColumnEvidence` below only fetches links for the single
// `chosenColumn`, and stacking other cited columns would mean fetching their
// evidence just to render them here too — the column picker already exists
// to choose which column's value shows, so the simplest composition keeps
// that single-column contract instead of widening the evidence fetch
// surface.
//
// The right pane's "extraction" section is intentionally DROPPED: the
// middle pane already renders the selected row's value + chips directly
// beside it, so repeating it inside the evidence pane would be pure
// duplication of the same DOM a few pixels to the right. The pane opens
// straight at the citation, then the media preview + segmented transcript.
// The middle pane is the single place the extraction value renders.

const LIST_PAGE_SIZE = 100;
const ANSWERS_EVIDENCE_MIN_WIDTH = 420;
const ANSWERS_EVIDENCE_MAX_WIDTH = 960;
const ANSWERS_EVIDENCE_DEFAULT_WIDTH = 600;

export interface AnswersViewProps {
  projectId: string;
  sheet: SheetMeta;
  /** The sheet's cited columns, already resolved to ColumnDef, in sheet
   *  column order. Always non-empty while this view is showing (availability
   *  gates on it). */
  citedColumns: readonly ColumnDef[];
  /** null = default (first cited column). */
  chosenColumnId: string | null;
  onChangeColumn(columnId: string): void;
  /** The docked pane's current source; null = nothing opened yet. */
  activeLinkId: string | number | null;
  onSetActiveLink(linkId: string | number | null): void;
  selectedRowIds: readonly string[];
  /** Write the shared SelectedGridRows selection — this view is ALWAYS
   *  synced (a lens on the current selection, not an independent browse
   *  cursor, unlike DocumentView's opt-in Sync). */
  onSyncSelectRow(rowId: string): void;
  queryRows(args: { columnIds?: string[]; offset: number; limit: number }): Promise<{
    rows: Row[];
    total: number;
  }>;
  orderKey: string;
  titleColumnOrder?: readonly string[];
}

interface ListState {
  key: string;
  rows: Row[];
  total: number;
  loading: boolean;
  error: string | null;
}

export function AnswersView({
  projectId,
  sheet,
  citedColumns,
  chosenColumnId,
  onChangeColumn,
  activeLinkId,
  onSetActiveLink,
  selectedRowIds,
  onSyncSelectRow,
  queryRows,
  orderKey,
  titleColumnOrder,
}: AnswersViewProps) {
  const { projectApi } = useWorkspaceStores();
  const evidenceResize = useResizable({
    storageKey: `frisket:answers-evidence-width:${projectId}`,
    minWidth: ANSWERS_EVIDENCE_MIN_WIDTH,
    maxWidth: ANSWERS_EVIDENCE_MAX_WIDTH,
    defaultWidth: ANSWERS_EVIDENCE_DEFAULT_WIDTH,
    handleEdge: 'left',
  });
  const chosenColumn = useMemo(
    () =>
      citedColumns.find((column) => column.id === chosenColumnId) ?? citedColumns[0] ?? null,
    [citedColumns, chosenColumnId],
  );

  const listKey = `${sheet.id}:${orderKey}`;
  const [list, setList] = useState<ListState>({
    key: listKey,
    rows: [],
    total: sheet.rowCount,
    loading: true,
    error: null,
  });
  const [search, setSearch] = useState('');

  // queryRows is a SERVER data-fetcher prop (App.tsx wires it to
  // api.getSheetData) — a Promise-returning data source, NOT a parent state
  // setter. Calling it here loads the first page when listKey/queryRows change
  // and forces no ancestor re-render, so this is the ordinary "fetch data in an
  // effect" pattern, not the shared-state-sync anti-pattern. no-prop-callback-
  // in-effect here is a false positive — allowlisted in doctor.config.ts.
  useEffect(() => {
    let cancelled = false;
    setList({ key: listKey, rows: [], total: sheet.rowCount, loading: true, error: null });
    void queryRows({ offset: 0, limit: LIST_PAGE_SIZE })
      .then((page) => {
        if (cancelled) return;
        setList({ key: listKey, rows: page.rows, total: page.total, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setList({
          key: listKey,
          rows: [],
          total: sheet.rowCount,
          loading: false,
          error: err instanceof Error ? err.message : 'Could not load rows.',
        });
      });
    return () => {
      cancelled = true;
    };
  }, [listKey, queryRows, sheet.rowCount]);

  const loadMore = useCallback(async () => {
    if (list.loading || list.rows.length >= list.total) return;
    setList((prev) => ({ ...prev, loading: true }));
    try {
      const page = await queryRows({ offset: list.rows.length, limit: LIST_PAGE_SIZE });
      setList((prev) => ({
        key: prev.key,
        rows: [...prev.rows, ...page.rows],
        total: page.total,
        loading: false,
        error: null,
      }));
    } catch (err) {
      setList((prev) => ({
        ...prev,
        loading: false,
        error: err instanceof Error ? err.message : 'Could not load rows.',
      }));
    }
  }, [list.loading, list.rows.length, list.total, queryRows]);

  const rowTitle = useCallback(
    (row: Row): string => resolveRowTitle(sheet, row, { columnOrder: titleColumnOrder }),
    [sheet, titleColumnOrder],
  );

  const filteredRows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return list.rows;
    return list.rows.filter((row) => rowTitle(row).toLowerCase().includes(needle));
  }, [list.rows, search, rowTitle]);

  // Always selection-synced — the active row IS the shared selection's first
  // entry, falling back to the first row so the middle pane always shows
  // something.
  const activeRowId = useMemo(() => {
    const ids = new Set(filteredRows.map((row) => row.id));
    const selected = selectedRowIds.find((id) => ids.has(id));
    if (selected) return selected;
    return filteredRows[0]?.id ?? null;
  }, [filteredRows, selectedRowIds]);

  const { listBodyRef, onListScroll, onListKeyDown, startIndex, windowRows } = useWindowedRowList({
    rows: filteredRows,
    activeId: activeRowId,
    onSelect: onSyncSelectRow,
  });

  // Column evidence as a KEYED ASYNC RESOURCE. One batch call per (column,
  // loaded rows), fetched on subscription miss and READ during render, not
  // an effect that setStates on prop change. The old mount-timing race between
  // an evidence-fetch effect and a default-link effect (and its `list.loading`
  // guard + `defaultedKeyRef` sentinel) is gone structurally: there is one
  // synchronously-readable snapshot, so there is no ordering between two effects
  // to get wrong. The key is `${sheet.id}:${chosenColumn.id}:${rowIds}`.
  const rowIds = useMemo(() => list.rows.map((row) => row.id), [list.rows]);
  const evidence = useColumnEvidence(sheet.id, chosenColumn?.id ?? null, rowIds, projectApi);

  // The default docked link ("the active row's first cited cell's first
  // link") is a PURE render-time derivation of the resource's current
  // value — not an effect chasing props, and not pushed to the parent from an
  // effect (that was the no-pass-live-state-to-parent finding). Null while
  // the batch is still pending so we never default off an empty-but-settled
  // snapshot.
  const defaultLinkId = useMemo(() => {
    if (!activeRowId || evidence.status === 'pending') return null;
    return evidence.byRow[activeRowId]?.[0]?.stable_id ?? null;
  }, [activeRowId, evidence]);

  // The user's explicit docked-pane interaction, scoped to the (column, row)
  // context it was made in. A chip click sets a link; closing the pane sets
  // null. Scoping by context is what lets "user closed the pane" stick for the
  // SAME row/column (no re-default) while moving to a different row/column
  // re-defaults — the behavior the deleted `defaultedKeyRef` sentinel encoded.
  const activeContext =
    chosenColumn && activeRowId ? `${sheet.id}:${chosenColumn.id}:${activeRowId}` : null;
  const [linkChoice, setLinkChoice] = useState<{
    context: string;
    linkId: string | number | null;
  } | null>(null);
  const chooseLink = useCallback(
    (linkId: string | number | null) => {
      setLinkChoice({ context: activeContext ?? '', linkId });
      // Mirror the explicit choice into the shared scratch slice (a chip's
      // onOpen sets answersView.activeLinkId) — an EVENT-handler write, never
      // an effect.
      onSetActiveLink(linkId);
    },
    [activeContext, onSetActiveLink],
  );

  // What the docked pane actually shows: an in-session explicit choice for the
  // current context wins; otherwise a persisted scratch value on first display
  // (grid<->answers remount); otherwise the derived default.
  const effectiveLinkId =
    linkChoice && linkChoice.context === activeContext
      ? linkChoice.linkId
      : linkChoice === null && activeLinkId !== null
        ? activeLinkId
        : defaultLinkId;

  // The selected row's own answer + its citations — the docked pane composes
  // a three-part reading of THIS row (show the extraction, the citation, then
  // the media preview + segmented transcript), not a raw viewer dump.
  const activeRow = useMemo(
    () => (activeRowId ? list.rows.find((row) => row.id === activeRowId) ?? null : null),
    [list.rows, activeRowId],
  );
  const activeLinks = activeRowId ? evidence.byRow[activeRowId] ?? [] : [];
  const activeLink = activeLinks.find((link) => link.stable_id === effectiveLinkId) ?? null;
  const activeValue = chosenColumn && activeRow ? activeRow.cells[chosenColumn.id] : null;

  // The header quote is the SAME full-citation derivation the middle pane's
  // chips use (citationQuote.ts), fetched once here for the pane's
  // currently-open link — see AnswersCitationChip below for the per-chip
  // sibling call.
  const activeLinkViewer = useEvidenceLinkViewer(effectiveLinkId, projectApi);
  const activeLinkQuote = mergeCitedQuote(activeLinkViewer.payload) ?? activeLink?.snippet ?? null;

  return (
    <div
      className={`answers-view${evidenceResize.resizing ? ' answers-view-resizing' : ''}`}
      data-testid="grounded-answers-view"
      data-project-id={projectId}
      style={{
        '--answers-evidence-width': `${evidenceResize.width}px`,
        '--answers-evidence-min-width': `${ANSWERS_EVIDENCE_MIN_WIDTH}px`,
        '--answers-evidence-max-width': `${ANSWERS_EVIDENCE_MAX_WIDTH}px`,
      } as CSSProperties}
    >
      <aside className="answers-row-list" data-testid="answers-row-list" aria-label="Rows">
        <div className="answers-row-list-search">
          <Search size={13} aria-hidden />
          <input
            data-testid="answers-row-search"
            placeholder="Search rows…"
            aria-label="Search rows"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        <div className="answers-row-list-count muted mono">
          {filteredRows.length.toLocaleString()} of {list.total.toLocaleString()}
        </div>
        <div
          className="answers-row-list-body drawer-body"
          data-testid="answers-row-list-body"
          ref={listBodyRef}
          onScroll={onListScroll}
          onKeyDown={onListKeyDown}
          tabIndex={0}
          role="listbox"
          aria-label="Row list"
        >
          {list.error && (
            <div className="answers-row-list-message" role="alert">
              {list.error}
            </div>
          )}
          {!list.loading && filteredRows.length === 0 && (
            <div className="answers-row-list-empty" data-testid="answers-row-list-empty">
              No rows match.
            </div>
          )}
          <div
            className="answers-row-list-scroller"
            style={{ height: `${filteredRows.length * LIST_ITEM_HEIGHT}px` }}
          >
            {windowRows.map((row, offset) => {
              const index = startIndex + offset;
              const active = row.id === activeRowId;
              return (
                <button
                  type="button"
                  key={row.id}
                  className={`answers-row-item${active ? ' active' : ''}`}
                  data-testid="answers-row-item"
                  data-row-id={row.id}
                  data-active={active ? 'true' : 'false'}
                  role="option"
                  aria-selected={active}
                  style={{
                    position: 'absolute',
                    top: `${index * LIST_ITEM_HEIGHT}px`,
                    height: `${LIST_ITEM_HEIGHT}px`,
                  }}
                  onClick={() => onSyncSelectRow(row.id)}
                >
                  <span className="answers-row-item-title">{rowTitle(row)}</span>
                </button>
              );
            })}
          </div>
          {list.rows.length < list.total && (
            <button
              type="button"
              className="mini-btn answers-row-list-more"
              data-testid="answers-row-list-more"
              disabled={list.loading}
              onClick={() => void loadMore()}
            >
              {list.loading ? 'Loading…' : 'Load more'}
            </button>
          )}
        </div>
      </aside>

      <section className="answers-column" data-testid="answers-column" aria-label="Answers">
        <div className="answers-column-header">
          {citedColumns.length > 1 ? (
            <label className="answers-column-picker-row">
              <span>Column</span>
              <PanelSelect
                className="row-height-select"
                data-testid="answers-column-picker"
                value={chosenColumn ? chosenColumn.id : ''}
                onChange={(event) => onChangeColumn(event.target.value)}
              >
                {citedColumns.map((column) => (
                  <option key={column.id} value={column.id}>
                    {column.name}
                  </option>
                ))}
              </PanelSelect>
            </label>
          ) : (
            <span className="answers-column-picker-static" data-testid="answers-column-picker">
              {chosenColumn?.name ?? ''}
            </span>
          )}
        </div>
        <div className="answers-column-body drawer-body">
          {/* SCOPED to the left rail's active row only — never the other
              loaded rows' values, even though `list.rows` holds every paged
              row for the left rail. */}
          {activeRow ? (
            <div
              key={activeRow.id}
              className="answers-cell active"
              data-testid="answers-cell"
              data-row-id={activeRow.id}
              data-active="true"
            >
              <div className="answers-cell-title">{rowTitle(activeRow)}</div>
              <div className="answers-cell-content" data-testid="answers-cell-content">
                {activeValue === null || activeValue === undefined || activeValue === '' ? (
                  <span className="row-field-empty">empty</span>
                ) : chosenColumn?.format === 'markdown' ? (
                  <MarkdownView source={String(activeValue)} className="answers-cell-markdown" />
                ) : (
                  String(activeValue)
                )}
              </div>
              {activeLinks.length > 0 && (
                <div className="answers-cell-chips" data-testid="answers-cell-chips">
                  {activeLinks.map((link, i) => (
                    <AnswersCitationChip
                      key={link.stable_id}
                      link={link}
                      index={i}
                      active={link.stable_id === effectiveLinkId}
                      onOpen={chooseLink}
                      linkApi={projectApi}
                    />
                  ))}
                </div>
              )}
            </div>
          ) : (
            !list.loading && <div className="answers-column-empty muted">No rows.</div>
          )}
        </div>
      </section>

      <ResizeSeam
        className="answers-evidence-seam"
        ariaLabel="Resize the Answers evidence pane"
        testId="answers-evidence-seam"
        width={evidenceResize.width}
        min={ANSWERS_EVIDENCE_MIN_WIDTH}
        max={ANSWERS_EVIDENCE_MAX_WIDTH}
        onResizeStart={evidenceResize.onResizeStart}
        onResizeKeyDown={evidenceResize.onResizeKeyDown}
      />

      <section
        className="answers-evidence-pane"
        data-testid="answers-evidence-pane"
        aria-label="Evidence"
      >
        {effectiveLinkId !== null ? (
          <>
            {/* 1. the citation itself — the quote/anchor the extraction cites.
                The extraction value+chips this citation supports render in
                the MIDDLE pane now, scoped to this same selected row, so this
                pane no longer repeats it. */}
            <div className="answers-evidence-citation" data-testid="answers-evidence-citation">
              <div className="answers-evidence-section-label">Citation</div>
              {activeLinkQuote ? (
                <blockquote data-testid="answers-evidence-citation-quote">{activeLinkQuote}</blockquote>
              ) : (
                <span className="muted">No quoted anchor for this citation.</span>
              )}
            </div>
            {/* 2. the media preview (seeked to the span) + the transcript in
                discrete SEGMENTS scoped to THIS row, cited segment highlighted
                — the docked EvidenceViewer, scoped via scopeRowId so it renders
                only the selected row's own source, never every video's. */}
            <Suspense fallback={<PanelLoading className="grid-host" label="Loading evidence…" />}>
              <LazyEvidenceViewer
                key={String(effectiveLinkId)}
                evidenceLinkId={effectiveLinkId}
                mode="pane"
                scopeRowId={activeRowId}
                onClose={() => chooseLink(null)}
                defaultShowDetails={false}
              />
            </Suspense>
          </>
        ) : (
          <div className="answers-evidence-empty" data-testid="answers-evidence-empty">
            Select a citation to view its source.
          </div>
        )}
      </section>
    </div>
  );
}

/** One middle-pane citation chip. Split out of the `activeLinks.map(...)`
 * above SOLELY so it can call
 * `useEvidenceLinkViewer` — a hook can't be called a variable number of
 * times inside a loop, but a variable number of sibling COMPONENT instances
 * each calling it once is exactly the Rules-of-Hooks-safe shape. Falls back
 * to the chip-summary's `link.snippet` (the old rank-0-span text) while the
 * full payload is loading or if it never resolves — never a blank chip. */
function AnswersCitationChip({
  link,
  index,
  active,
  onOpen,
  linkApi,
}: {
  link: CellEvidenceLinkSummary;
  index: number;
  active: boolean;
  onOpen: (linkId: string) => void;
  linkApi: EvidenceLinkViewerApiPort;
}) {
  const viewer = useEvidenceLinkViewer(link.stable_id, linkApi);
  const fullQuote = mergeCitedQuote(viewer.payload) ?? link.snippet;
  return (
    <CitationChip
      linkId={link.stable_id}
      label={`Citation ${index + 1}`}
      snippet={fullQuote}
      onOpen={onOpen}
      testId="answers-citation-chip"
      className={active ? 'cell-evidence-link-active' : undefined}
    />
  );
}
