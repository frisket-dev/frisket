import { Search } from 'lucide-react';
import type { CSSProperties } from 'react';
import type { Row, SheetMeta } from '../api/types';
import { MenuPop } from '../components/MenuPop';
import type { DocumentViewState } from '../workspace/useWorkspaceChromeState';
import { useMemo, useRef, useState } from 'react';
import { AnnotatedTextReader } from './AnnotatedTextReader';
import { MentionDetailPanel } from './MentionDetailPanel';
import { mentionTargetForMark, type MentionDetailTarget } from './mentionDetailModel';
import { documentMediaKind } from './documentMedia';
import { DocumentReader } from './DocumentReader';
import { DerivedColumnPane } from './DerivedColumnPane';
import type { TimedTranscriptDocument } from './timedTranscriptModel';
import { useDocumentView } from './useDocumentView';
import { LIST_ITEM_HEIGHT } from './useWindowedRowList';
import { PanelLoading } from '../components/PanelPrimitives';
import { PanelSelect } from '../components/PanelSelect';

interface DocumentViewProps {
  projectId: string;
  sheet: SheetMeta;
  state: DocumentViewState;
  onChangeState(next: DocumentViewState): void;
  /** Keep an already-open Detail reader aligned with the focused document. */
  onDocumentFocus(rowId: string): void;
  /** Open the resident Detail column for a row (openRowById): selects + docks. */
  onOpenDetail(rowId: string): void;
  queryRows(args: { columnIds?: string[]; offset: number; limit: number }): Promise<{
    rows: Row[];
    total: number;
  }>;
  /** Drives the SHEET's grid sort on the title column (list stays the sheet's
   *  order — the list never re-implements its own sort/filter). */
  onListSort(columnName: string, dir: 'asc' | 'desc' | null): void;
  /** Serialized active filter+sort so the list re-queries when the sheet order
   *  changes. */
  orderKey: string;
  /** The title column's current sheet-sort direction, or null. */
  listSortDir: 'asc' | 'desc' | null;
  /** The grid's current column drag order (NAMES, visible-only) — the
   *  default-column input to `rowTitle()`. Absent/empty falls back to
   *  canonical column order. */
  titleColumnOrder?: readonly string[];
  /** SheetMeta.annotatedTextColumnIds: which text columns can be READ here. */
  annotatedTextColumnIds: readonly string[];
  /** Persisted per-sheet disabled annotation layers (chrome state, D3). */
  disabledToggleKeys: readonly string[];
  onSetDisabledToggleKeys(next: string[]): void;
  /** Replay the stored `map.ner` action behind a stale layer, from its output
   *  column. Absent when the host cannot launch runs. */
  onReplayAnnotationLayer?: ((outputColumnId: string) => void) | null;
}

export function DocumentView({
  projectId,
  sheet,
  state,
  onChangeState,
  onDocumentFocus,
  onOpenDetail,
  queryRows,
  onListSort,
  orderKey,
  listSortDir,
  titleColumnOrder,
  annotatedTextColumnIds,
  disabledToggleKeys,
  onSetDisabledToggleKeys,
  onReplayAnnotationLayer,
}: DocumentViewProps) {
  // The docked mention-detail panel (O3): opening a document from it keeps it
  // open, so a reader can walk the results without losing the list.
  const [mention, setMention] = useState<{
    target: MentionDetailTarget;
    occurrenceId: string;
  } | null>(null);
  const [derivedColumnId, setDerivedColumnId] = useState<string | null>(null);
  // Each view-option select portals its custom menu to <body>, so the menu is
  // NOT a DOM descendant of the options popover. Two consequences, both
  // handled by the props threaded below: the menu must be promoted to the top
  // layer itself (`topLayer`) or the popover — which is already in the top
  // layer — paints over it, and the popover's outside-pointerdown dismissal
  // must count these menus as "inside" (`extraRefs`) or choosing an option
  // dismisses the popover the option belongs to.
  const optionSourceMenuRef = useRef<HTMLDivElement>(null);
  const optionTitleMenuRef = useRef<HTMLDivElement>(null);
  const optionLayoutMenuRef = useRef<HTMLDivElement>(null);
  const optionFitMenuRef = useRef<HTMLDivElement>(null);
  const optionListSortMenuRef = useRef<HTMLDivElement>(null);
  const optionMenuRefs = useMemo(
    () => [
      optionSourceMenuRef,
      optionTitleMenuRef,
      optionLayoutMenuRef,
      optionFitMenuRef,
      optionListSortMenuRef,
    ],
    [],
  );
  const {
    sources,
    source,
    sourceColumn,
    defaultTitleColumn,
    titleColumn,
    list,
    pageCounts,
    search,
    setSearch,
    optionsOpen,
    setOptionsOpen,
    listBodyRef,
    filteredRows,
    activeRowId,
    activeRow,
    activeMedia,
    activeMediaKind,
    resolveRowMedia,
    rowTitle,
    recordPageCount,
    selectDocument,
    onListKeyDown,
    onListScroll,
    startIndex,
    windowRows,
    loadMore,
    setState,
  } = useDocumentView({
    projectId,
    sheet,
    state,
    onChangeState,
    onDocumentFocus,
    queryRows,
    orderKey,
    titleColumnOrder,
    annotatedTextColumnIds,
  });

  const activeTimedTranscriptDocument = useMemo<TimedTranscriptDocument | null>(() => {
    if (
      !activeRow
      || !activeMedia
      || (activeMediaKind !== 'audio' && activeMediaKind !== 'video')
    ) return null;
    return {
      row: activeRow,
      sheet,
      media: activeMedia,
      kind: activeMediaKind,
      title: rowTitle(activeRow, activeMedia),
      videoClassName: activeMediaKind === 'video' ? `document-video-${state.videoFit}` : undefined,
    };
  }, [activeMedia, activeMediaKind, activeRow, rowTitle, sheet, state.videoFit]);

  if (list.loading && list.rows.length === 0) {
    return (
      <div
        className="document-view"
        data-testid="document-view"
        data-project-id={projectId}
        data-source-column-id={sourceColumn ? String(sourceColumn.id) : ''}
      >
        <PanelLoading
          className="main-view-loading"
          testId="document-view-loading"
          label="Loading documents…"
        />
      </div>
    );
  }

  // Render-prop, not a pre-built element (DocumentReader.tsx owns the popover
  // ref + top-layer placement style alongside its trigger; this attaches them
  // via ordinary JSX so neither side touches a ref outside JSX).
  const optionsPopover = ({ popoverRef, style }: { popoverRef: (el: HTMLDivElement | null) => void; style: CSSProperties }) => (
    <MenuPop ref={popoverRef} style={style} className="document-options" role="dialog" data-testid="document-options" aria-label="View options">
      {sources.length > 1 && (
        <label className="document-option-row">
          <span>Source</span>
          <PanelSelect
            className="row-height-select"
            data-testid="document-option-source"
            topLayer
            menuRef={optionSourceMenuRef}
            value={sourceColumn ? String(sourceColumn.id) : ''}
            onChange={(event) => setState({ sourceColumnId: event.target.value })}
          >
            {/* Media AND annotated-text columns in one list: they are the same
                question ("what am I reading?"), and splitting them into two
                controls would make the reader's identity a mode. */}
            {sources.map((entry) => (
              <option key={entry.column.id} value={String(entry.column.id)}>
                {entry.column.name}
                {entry.kind === 'text' ? ' (text)' : ''}
              </option>
            ))}
          </PanelSelect>
        </label>
      )}
      <label className="document-option-row">
        {/* This is a per-view OVERRIDE of the sheet's default title column
            (set sheet-wide from the column '...' menu's "Use as row title")
            — the blank option restores that default instead of pinning a
            column forever. */}
        <span>Title override</span>
        <PanelSelect
          className="row-height-select"
          data-testid="document-option-title"
            topLayer
            menuRef={optionTitleMenuRef}
          value={state.titleColumnId ?? ''}
          onChange={(event) =>
            setState({ titleColumnId: event.target.value === '' ? null : event.target.value })
          }
        >
          <option value="">{`Sheet default (${defaultTitleColumn?.name ?? 'first column'})`}</option>
          {sheet.columns.map((column) => (
            <option key={column.id} value={String(column.id)}>
              {column.name}
            </option>
          ))}
        </PanelSelect>
      </label>
      {/* Layout / Fit are PDF page-rendering options; a text source has no
          pages, so offering them would be two controls that do nothing. List
          sort and Sync below are source-independent and stay. */}
      {source?.kind !== 'text' && (
        <>
      <label className="document-option-row">
        <span>Layout</span>
        <PanelSelect
          className="row-height-select"
          data-testid="document-option-layout"
            topLayer
            menuRef={optionLayoutMenuRef}
          value={state.layout}
          onChange={(event) =>
            setState({ layout: event.target.value as DocumentViewState['layout'] })
          }
        >
          <option value="continuous">Continuous</option>
          <option value="single">Single page</option>
          <option value="two-up">Two-up</option>
        </PanelSelect>
      </label>
      <label className="document-option-row">
        <span>Fit</span>
        <PanelSelect
          className="row-height-select"
          data-testid="document-option-fit"
            topLayer
            menuRef={optionFitMenuRef}
          value={state.fit}
          onChange={(event) => setState({ fit: event.target.value as DocumentViewState['fit'] })}
        >
          <option value="width">Fit width</option>
          <option value="page">Fit page</option>
        </PanelSelect>
      </label>
      <label className="document-option-row document-option-check">
        <input
          type="checkbox"
          data-testid="document-option-textlayer"
          checked={state.textLayer}
          onChange={(event) => setState({ textLayer: event.target.checked })}
        />
        <span>Text/OCR overlay</span>
      </label>
        </>
      )}
      <label className="document-option-row">
        <span>List sort</span>
        <PanelSelect
          className="row-height-select"
          data-testid="document-option-listsort"
            topLayer
            menuRef={optionListSortMenuRef}
          value={listSortDir ?? 'none'}
          onChange={(event) => {
            const value = event.target.value as 'asc' | 'desc' | 'none';
            if (titleColumn) onListSort(titleColumn.name, value === 'none' ? null : value);
          }}
        >
          <option value="none">Sheet order</option>
          <option value="asc">Title A→Z</option>
          <option value="desc">Title Z→A</option>
        </PanelSelect>
      </label>
    </MenuPop>
  );

  return (
    <div
      className="document-view"
      data-testid="document-view"
      data-project-id={projectId}
      data-source-column-id={sourceColumn ? String(sourceColumn.id) : ''}
    >
      <aside className="document-list" data-testid="document-list" aria-label="Documents">
        <div className="document-list-search">
          <Search size={13} aria-hidden />
          <input
            data-testid="document-list-search"
            placeholder="Search documents…"
            aria-label="Search documents"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        <div className="document-list-count muted mono">
          {filteredRows.length.toLocaleString()} of {list.total.toLocaleString()}
        </div>
        <div
          className="document-list-body drawer-body"
          data-testid="document-list-body"
          ref={listBodyRef}
          onScroll={onListScroll}
          onKeyDown={onListKeyDown}
          tabIndex={0}
          role="listbox"
          aria-label="Document list"
        >
          {list.error && (
            <div className="document-list-message" role="alert">
              {list.error}
            </div>
          )}
          {!list.loading && filteredRows.length === 0 && (
            <div className="document-list-empty" data-testid="document-list-empty">
              No documents match.
            </div>
          )}
          <div
            className="document-list-scroller"
            style={{ height: `${filteredRows.length * LIST_ITEM_HEIGHT}px` }}
          >
            {windowRows.map((row, offset) => {
              const index = startIndex + offset;
              const media = resolveRowMedia(row);
              const rowId = String(row.id);
              const active = rowId === activeRowId;
              const count = pageCounts[rowId];
              // A text source has no media and no pages; its honest secondary
              // line is the size of the thing you are about to read.
              const textLength =
                source?.kind === 'text'
                  ? String(row.cells[String(source.column.id)] ?? '').length
                  : null;
              const secondary =
                textLength !== null
                  ? textLength > 0
                    ? `${textLength.toLocaleString()} characters`
                    : 'Empty'
                  : count != null
                    ? `${count} page${count === 1 ? '' : 's'}`
                    : media
                      ? documentMediaKind(media, sourceColumn?.type ?? 'file') === 'pdf'
                        ? 'PDF'
                        : (media.mime ?? 'File')
                      : 'No document';
              return (
                <button
                  type="button"
                  key={rowId}
                  className={`document-list-item${active ? ' active' : ''}`}
                  data-testid="document-list-item"
                  data-row-id={rowId}
                  data-active={active ? 'true' : 'false'}
                  data-has-media={media ? 'true' : 'false'}
                  role="option"
                  aria-selected={active}
                  style={{ position: 'absolute', top: `${index * LIST_ITEM_HEIGHT}px`, height: `${LIST_ITEM_HEIGHT}px` }}
                  onClick={() => selectDocument(rowId)}
                  onDoubleClick={() => onOpenDetail(rowId)}
                >
                  <span className="document-list-item-title">{rowTitle(row, media)}</span>
                  <span className="document-list-item-secondary muted">{secondary}</span>
                </button>
              );
            })}
          </div>
          {list.rows.length < list.total && (
            <button
              type="button"
              className="mini-btn document-list-more"
              data-testid="document-list-more"
              disabled={list.loading}
              onClick={() => void loadMore()}
            >
              {list.loading ? 'Loading…' : 'Load more'}
            </button>
          )}
        </div>
      </aside>
      {source?.kind === 'text' ? (
        <AnnotatedTextReader
          key={`text-reader-${activeRowId ?? 'none'}`}
          rowId={activeRowId}
          columnId={String(source.column.id)}
          title={activeRow ? rowTitle(activeRow, null) : 'No document selected'}
          disabledToggleKeys={disabledToggleKeys}
          onSetDisabledToggleKeys={onSetDisabledToggleKeys}
          onOpenMention={(mark) => {
            const target = mentionTargetForMark(mark, String(sheet.id));
            setMention(target ? { target, occurrenceId: mark.occurrenceId } : null);
          }}
          activeOccurrenceId={mention?.occurrenceId ?? null}
          onOpenDetail={() => {
            if (activeRowId) onOpenDetail(activeRowId);
          }}
          canOpenDetail={Boolean(activeRowId)}
          optionsOpen={optionsOpen}
          onToggleOptions={() => setOptionsOpen((open) => !open)}
          optionsPopover={optionsPopover}
          optionsMenuRefs={optionMenuRefs}
          selectionCount={0}
          onReplayLayer={onReplayAnnotationLayer ?? null}
        />
      ) : (
        <DocumentReader
          key={`reader-${activeRowId ?? 'none'}`}
          media={activeMedia}
          mediaKind={activeMediaKind}
          title={activeRow ? rowTitle(activeRow, activeMedia) : 'No document selected'}
          layout={state.layout}
          fit={state.fit}
          videoFit={state.videoFit}
          onVideoFitChange={(videoFit) => setState({ videoFit })}
          textLayer={state.textLayer}
          onPageCount={recordPageCount}
          rowKey={activeRowId ?? 'none'}
          onOpenDetail={() => {
            if (activeRowId) onOpenDetail(activeRowId);
          }}
          canOpenDetail={Boolean(activeRowId)}
          optionsOpen={optionsOpen}
          onToggleOptions={() => setOptionsOpen((open) => !open)}
          optionsPopover={optionsPopover}
          optionsMenuRefs={optionMenuRefs}
          selectionCount={0}
          timedTranscriptDocument={activeTimedTranscriptDocument}
        />
      )}
      <DerivedColumnPane
        columns={sheet.columns}
        row={activeRow}
        selectedColumnId={derivedColumnId}
        onChangeColumn={setDerivedColumnId}
      />
      {mention !== null && source?.kind === 'text' && (
        <MentionDetailPanel
          target={mention.target}
          activeRowId={activeRowId}
          onClose={() => setMention(null)}
          onOpenDocument={(rowId, occurrenceId) => {
            selectDocument(rowId);
            // Clicking an OCCURRENCE aims the reader at that mark; clicking the
            // document title just opens it. The panel stays open either way
            // (O3) — that is what makes this a drill-down and not a navigation.
            if (occurrenceId !== undefined) {
              setMention((open) => (open === null ? null : { ...open, occurrenceId }));
            }
          }}
        />
      )}
    </div>
  );
}
