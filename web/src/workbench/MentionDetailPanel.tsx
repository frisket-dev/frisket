// The drill-down the Mentions panel lacks. Clicking a mention group there
// filters the GRID; this answers the question a reader actually has — which
// documents does this mention appear in, and how many times in each — without
// navigating away from what you were reading.
//
// Copy says "the same normalized mention", never "the same
// entity". The fingerprint groups SPELLINGS; it does not resolve identities and
// there is no coreference here. The unfingerprinted types (date, money,
// quantity, …) are not merged across spellings at all, so the panel says which
// mode it is in rather than implying one.
import { useCallback, useEffect, useState } from 'react';
import { ChevronDown, ChevronRight, Filter, X } from 'lucide-react';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type {
  EntityMentionDocumentsPage,
  EntityMentionOccurrencesPage,
  GridFilterEntityValue,
} from '../api/types';
import {
  mentionDetailFilterValue,
  mentionDetailKey,
  snippetParts,
  type MentionDetailTarget,
} from './mentionDetailModel';
import { unpositionedExplanation } from './textAnnotationModel';
import { entityTypeName } from '../components/action-panel/nerLabelModel';
import { PanelHeader } from '../components/PanelPrimitives';

const PAGE_SIZE = 25;
/** Occurrences per Level-2 page. Smaller than the documents page because these
 *  carry text: a document with 200 mentions shows K of them and offers the
 *  rest (R-snippet-cost). */
const OCCURRENCE_PAGE_SIZE = 10;

interface PageState {
  key: string;
  page: EntityMentionDocumentsPage | null;
  loading: boolean;
  error: string | null;
}

/** Occurrences for one document. Absent until the document is expanded so a
 *  mention in 300 documents costs 300 rows, not 300 cell fetches. */
interface OccurrenceState {
  page: EntityMentionOccurrencesPage | null;
  loading: boolean;
  error: string | null;
}

interface ExpandedState {
  /** The mention these open documents belong to. */
  key: string;
  byRow: Record<string, OccurrenceState>;
}

/** Stable identity so `byRow` does not churn for a freshly-keyed panel. */
const EMPTY_ROWS: Record<string, OccurrenceState> = {};

export function MentionDetailPanel({
  target,
  activeRowId,
  onClose,
  onOpenDocument,
  onFilterGrid,
}: {
  target: MentionDetailTarget;
  /** The document currently open in the reader, so its row reads as current. */
  activeRowId: string | null;
  onClose(): void;
  /** Click a result: open that document's reader. The panel stays docked (O3)
   *  — a drill-down that closes itself on every click is a navigation, not a
   *  drill-down. `occurrenceId` is present when an OCCURRENCE was clicked, so
   *  the reader can open at that mark rather than at the top. */
  onOpenDocument(rowId: string, occurrenceId?: string): void;
  /** The Mentions panel's existing affordance, kept as a SEPARATE control
   *  rather than replaced by this panel (O3). */
  onFilterGrid?: ((value: GridFilterEntityValue) => void) | null;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const key = mentionDetailKey(target);
  const [state, setState] = useState<PageState>({
    key,
    page: null,
    loading: true,
    error: null,
  });

  useEffect(() => {
    let cancelled = false;
    void api
      .entityMentionDocuments({
        sheetId: target.sheetId,
        columnId: target.columnId,
        type: target.type,
        ...(target.fingerprint !== null
          ? { fingerprint: target.fingerprint }
          : { text: target.label }),
        limit: PAGE_SIZE,
      })
      .then((page) => {
        if (cancelled) return;
        setState({ key, page, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setState({
          key,
          page: null,
          loading: false,
          error: err instanceof Error ? err.message : 'Could not load this mention.',
        });
      });
    return () => {
      cancelled = true;
    };
  }, [key, target.sheetId, target.columnId, target.type, target.fingerprint, target.label]);

  // The open documents carry the mention they were opened for (the same keyed-
  // state idiom the reader uses), so switching mentions cannot show one
  // mention's occurrences under another's document row.
  const [expanded, setExpanded] = useState<ExpandedState>({ key, byRow: {} });
  const byRow = expanded.key === key ? expanded.byRow : EMPTY_ROWS;

  const current = state.key === key ? state : null;
  const page = current?.page ?? null;

  const patchRow = useCallback(
    (rowId: string, next: (prev: OccurrenceState | undefined) => OccurrenceState) => {
      setExpanded((prev) => {
        const base = prev.key === key ? prev.byRow : EMPTY_ROWS;
        return { key, byRow: { ...base, [rowId]: next(base[rowId]) } };
      });
    },
    [key],
  );

  const fetchOccurrences = useCallback(
    (rowId: string, offset: number) => {
      patchRow(rowId, (prev) => ({ page: prev?.page ?? null, loading: true, error: null }));
      void api
        .entityMentionOccurrences({
          sheetId: target.sheetId,
          rowId,
          columnId: target.columnId,
          type: target.type,
          ...(target.fingerprint !== null
            ? { fingerprint: target.fingerprint }
            : { text: target.label }),
          limit: OCCURRENCE_PAGE_SIZE,
          offset,
        })
        .then((next) => {
          patchRow(rowId, (prev) => ({
            // Only the list grows; totals and the unpositioned state are
            // whole-document facts.
            page: prev?.page && offset > 0
              ? { ...next, occurrences: [...prev.page.occurrences, ...next.occurrences] }
              : next,
            loading: false,
            error: null,
          }));
        })
        .catch((err: unknown) => {
          patchRow(rowId, (prev) => ({
            page: prev?.page ?? null,
            loading: false,
            error: err instanceof Error ? err.message : 'Could not load these mentions.',
          }));
        });
    },
    [patchRow, target.sheetId, target.columnId, target.type, target.fingerprint, target.label],
  );

  const toggleDocument = useCallback(
    (rowId: string) => {
      if (byRow[rowId]) {
        setExpanded((prev) => {
          const rest = { ...(prev.key === key ? prev.byRow : EMPTY_ROWS) };
          delete rest[rowId];
          return { key, byRow: rest };
        });
        return;
      }
      fetchOccurrences(rowId, 0);
    },
    [byRow, key, fetchOccurrences],
  );

  const loadMore = () => {
    if (page?.nextOffset == null || state.loading) return;
    setState((prev) => ({ ...prev, loading: true }));
    void api
      .entityMentionDocuments({
        sheetId: target.sheetId,
        columnId: target.columnId,
        type: target.type,
        ...(target.fingerprint !== null
          ? { fingerprint: target.fingerprint }
          : { text: target.label }),
        limit: PAGE_SIZE,
        offset: page.nextOffset,
      })
      .then((next) => {
        setState((prev) => ({
          key: prev.key,
          // Totals are full-group facts; only the document list grows.
          page: prev.page
            ? { ...next, documents: [...prev.page.documents, ...next.documents] }
            : next,
          loading: false,
          error: null,
        }));
      })
      .catch((err: unknown) => {
        setState((prev) => ({
          ...prev,
          loading: false,
          error: err instanceof Error ? err.message : 'Could not load more documents.',
        }));
      });
  };

  const totals = page?.totals ?? null;
  return (
    <aside className="mention-detail" data-testid="mention-detail-panel" aria-label="Mention detail">
      <PanelHeader
        className="panel-frame-header"
        testId="mention-detail-header"
        title={
          <span className="mention-detail-title" data-testid="mention-detail-title">
            {/* The clicked SPELLING, quoted, plus its type. Never the
                fingerprint: that is an internal comparison token, not a
                spelling anyone wrote. */}
            <span className="mention-detail-label">“{target.label}”</span>
            <span className="muted"> · {entityTypeName(target.type)}</span>
          </span>
        }
        actions={
          <>
            {onFilterGrid && (
              <button
                type="button"
                className="icon-btn"
                data-testid="mention-detail-filter"
                aria-label={`Filter the grid to rows containing “${target.label}”`}
                title={`Filter the grid to rows containing “${target.label}”`}
                onClick={() => onFilterGrid(mentionDetailFilterValue(target))}
              >
                <Filter size={13} />
              </button>
            )}
            <button
              type="button"
              className="icon-btn"
              data-testid="mention-detail-close"
              aria-label="Close mention detail"
              onClick={onClose}
            >
              <X size={14} />
            </button>
          </>
        }
      />
      <p className="mention-detail-counts" data-testid="mention-detail-counts">
        {totals === null
          ? '…'
          // The Mentions panel's own two numbers, from the same stream:
          // mention_count (occurrences) then row_count (documents).
          : `${totals.mentions.toLocaleString()} mention${totals.mentions === 1 ? '' : 's'} across ${totals.documents.toLocaleString()} document${totals.documents === 1 ? '' : 's'}`}
      </p>
      <p className="mention-detail-basis muted" data-testid="mention-detail-basis">
        {target.fingerprint !== null
          ? 'Every spelling that normalizes to this mention.'
          : 'This exact spelling. Dates, amounts and quantities are never merged across spellings.'}
      </p>
      <div className="mention-detail-body drawer-body">
        {current?.error != null && (
          <div className="panel-error" role="alert" data-testid="mention-detail-error">
            {current.error}
          </div>
        )}
        {page !== null && page.documents.length === 0 && (
          <p className="muted" data-testid="mention-detail-empty">
            No documents contain this mention.
          </p>
        )}
        <ul className="mention-detail-list" data-testid="mention-detail-list">
          {(page?.documents ?? []).map((document) => {
            const open = byRow[document.rowId];
            return (
              <li key={document.rowId}>
                <div className="mention-detail-doc-row">
                  {/* Two controls, because they are two questions: the
                      disclosure asks "where in this one" WITHOUT leaving the
                      document you are reading; the title opens it. Collapsing
                      them into one would make every peek a navigation. */}
                  <button
                    type="button"
                    className="mention-detail-doc-toggle"
                    data-testid="mention-detail-expand"
                    data-row-id={document.rowId}
                    aria-expanded={Boolean(open)}
                    aria-label={`${open ? 'Hide' : 'Show'} the ${document.occurrenceCount.toLocaleString()} mentions in ${document.title ?? `row ${document.rowId}`}`}
                    onClick={() => toggleDocument(document.rowId)}
                  >
                    {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                  </button>
                  <button
                    type="button"
                    className={`mention-detail-doc${document.rowId === activeRowId ? ' is-active' : ''}`}
                    data-testid="mention-detail-doc"
                    data-row-id={document.rowId}
                    aria-current={document.rowId === activeRowId}
                    onClick={() => onOpenDocument(document.rowId)}
                  >
                    <span className="mention-detail-doc-title">
                      {/* A row whose title column is empty is still a real
                          result; it gets its id rather than being dropped or
                          left blank. */}
                      {document.title ?? `Row ${document.rowId}`}
                    </span>
                    <span className="facet-count">
                      {document.occurrenceCount.toLocaleString()}
                    </span>
                  </button>
                </div>
                {open && (
                  <MentionOccurrenceList
                    state={open}
                    rowId={document.rowId}
                    onOpenOccurrence={onOpenDocument}
                    onLoadMore={(offset) => fetchOccurrences(document.rowId, offset)}
                  />
                )}
              </li>
            );
          })}
        </ul>
        {page?.nextOffset != null && (
          <button
            type="button"
            className="mini-btn"
            data-testid="mention-detail-more"
            disabled={state.loading}
            onClick={loadMore}
          >
            {state.loading
              ? 'Loading…'
              : `See ${Math.min(PAGE_SIZE, page.totals.documents - page.documents.length).toLocaleString()} more documents`}
          </button>
        )}
      </div>
    </aside>
  );
}

/** Where inside one document the mention sits, in reading order.
 *
 *  The snippet is drawn at the offsets the route returned over the text the
 *  route returned. When the layer's coordinates stopped applying, the route
 *  sends the reason and no occurrences, and this says so — a snippet cut at
 *  stale offsets is worse here than in the reader, because out of context
 *  nobody can see it landed in the wrong place. */
function MentionOccurrenceList({
  state,
  rowId,
  onOpenOccurrence,
  onLoadMore,
}: {
  state: OccurrenceState;
  rowId: string;
  onOpenOccurrence(rowId: string, occurrenceId: string): void;
  onLoadMore(offset: number): void;
}) {
  const page = state.page;
  if (state.error !== null) {
    return (
      <p className="panel-error" role="alert" data-testid="mention-occurrence-error">
        {state.error}
      </p>
    );
  }
  if (page === null) {
    return (
      <p className="muted mention-occurrence-note" data-testid="mention-occurrence-loading">
        Loading…
      </p>
    );
  }
  if (page.unpositioned !== null) {
    return (
      <p className="muted mention-occurrence-note" data-testid="mention-occurrence-unpositioned">
        {unpositionedExplanation(page.unpositioned.reason)}
      </p>
    );
  }
  if (page.occurrences.length === 0) {
    return (
      <p className="muted mention-occurrence-note" data-testid="mention-occurrence-empty">
        This document records no marked position for this mention.
      </p>
    );
  }
  const remaining = page.totals.occurrences - page.occurrences.length;
  return (
    <>
      <ol className="mention-occurrence-list" data-testid="mention-occurrence-list">
        {page.occurrences.map((occurrence) => {
          const parts = snippetParts(occurrence);
          return (
            <li key={occurrence.occurrenceId}>
              <button
                type="button"
                className="mention-occurrence"
                data-testid="mention-occurrence"
                data-occurrence-id={occurrence.occurrenceId}
                onClick={() => onOpenOccurrence(rowId, occurrence.occurrenceId)}
              >
                {parts.truncatedStart && <span aria-hidden>…</span>}
                {parts.before}
                <mark className="mention-occurrence-mark">{parts.mark}</mark>
                {parts.after}
                {parts.truncatedEnd && <span aria-hidden>…</span>}
              </button>
            </li>
          );
        })}
      </ol>
      {page.nextOffset != null && (
        <button
          type="button"
          className="mini-btn mention-occurrence-more"
          data-testid="mention-occurrence-more"
          disabled={state.loading}
          onClick={() => onLoadMore(page.nextOffset ?? 0)}
        >
          {state.loading
            ? 'Loading…'
            : `See ${remaining.toLocaleString()} more in this document`}
        </button>
      )}
    </>
  );
}
