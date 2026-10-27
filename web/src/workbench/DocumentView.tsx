import '../components/PanelPrimitives.css';
import '../components/BoundedTextReader.css';
import './DocumentView.css';
import '../components/evidence/EvidenceReading.css';
import { FormField } from '../components/FormControls';
import { DocumentList } from './DocumentList';
import type { CSSProperties } from 'react';
import type { DocumentListPage, Row, SheetMeta } from '../api/types';
import { MenuPop } from '../components/MenuPop';
import type { DocumentViewState } from '../workspace/useWorkspaceChromeState';
import { useEffect, useMemo, useRef, useState } from 'react';
import { AnnotatedTextReader } from './AnnotatedTextReader';
import { MentionDetailPanel } from './MentionDetailPanel';
import { mentionTargetForMark, type MentionDetailTarget } from './mentionDetailModel';
import {
  DocumentReader,
  type DocumentFindSimilarTarget,
} from './DocumentReader';
import { DocumentAlongsidePane } from './DocumentAlongsidePane';
import { useDocumentAlongsidePreference } from '../workspace/useWorkspaceChromeState';
import type { TimedTranscriptDocument } from './timedTranscriptModel';
import { useDocumentView } from './useDocumentView';
import { PanelLoading } from '../components/PanelPrimitives';
import { PanelSelect } from '../components/PanelSelect';
import { mediaFilename } from '../media/resolveMediaValue';

function filePage(value: unknown): number {
  if (typeof value !== 'string' || value[0] !== '{') return 1;
  try {
    const page = (JSON.parse(value) as { page?: unknown }).page;
    return typeof page === 'number' && Number.isInteger(page) && page >= 1 ? page : 1;
  } catch {
    return 1;
  }
}

interface DocumentViewProps {
  projectId: string;
  sheet: SheetMeta;
  state: DocumentViewState;
  onChangeState(next: DocumentViewState): void;
  /** Keep an already-open Detail reader aligned with the focused document. */
  onDocumentFocus(rowId: string): void;
  onSplitPdf?(rowId: string, columnId: string): void;
  /** Open the resident Detail column for a row (openRowById): selects + docks. */
  onOpenDetail(rowId: string): void;
  onFindSimilar?(anchor: {
    sheetId: string;
    rowId: string;
    columnId: string;
    target: DocumentFindSimilarTarget;
  }): void;
  queryDocuments(args: { sourceColumnId: string; titleColumnId: string | null; query: string;
    cursor?: string; anchorRowId?: string; limit: number }): Promise<DocumentListPage>;
  hydrateRow(rowId: string, columnIds: string[]): Promise<Row | null>;
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
  /** Transient state set only after the explicit-open priority endpoint accepts the row. */
  priorityReadingRowId?: string | null;
  onRequestDocumentPriority?(rowId: string, columnId: string): void;
  onPriorityLabel?(rowId: string, label: string): void;
}

export function DocumentView({
  projectId,
  sheet,
  state,
  onChangeState,
  onDocumentFocus,
  onSplitPdf,
  onOpenDetail,
  onFindSimilar,
  queryDocuments,
  hydrateRow,
  onListSort,
  orderKey,
  listSortDir,
  titleColumnOrder,
  annotatedTextColumnIds,
  disabledToggleKeys,
  onSetDisabledToggleKeys,
  onReplayAnnotationLayer,
  priorityReadingRowId = null,
  onRequestDocumentPriority,
  onPriorityLabel,
}: DocumentViewProps) {
  // The docked mention-detail panel (O3): opening a document from it keeps it
  // open, so a reader can walk the results without losing the list.
  const [mention, setMention] = useState<{
    target: MentionDetailTarget;
    occurrenceId: string;
  } | null>(null);
  const { preference: alongside, setPreference: setAlongside } = useDocumentAlongsidePreference(sheet.id);
  const alongsideTriggerRef = useRef<HTMLButtonElement>(null);
  const wasAlongsideOpen = useRef(alongside.open);
  useEffect(() => {
    if (wasAlongsideOpen.current && !alongside.open) alongsideTriggerRef.current?.focus();
    wasAlongsideOpen.current = alongside.open;
  }, [alongside.open]);
  // Each view-option select portals its custom menu to <body>, so the menu is
  // NOT a DOM descendant of the options popover. Two consequences, both
  // handled by shared top-layer menus and the refs below: the popover's dismissal
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
  const browse = useDocumentView({ projectId, sheet, state, onChangeState, onDocumentFocus,
    queryDocuments, hydrateRow, orderKey, titleColumnOrder, annotatedTextColumnIds });
  const {
    sources,
    source,
    sourceColumn,
    defaultTitleColumn,
    titleColumn,
    list,
    pageCounts,
    optionsOpen,
    setOptionsOpen,
    activeRowId,
    activeItem,
    activeRow,
    activeMedia,
    activeMediaKind,
    hydrationLoading,
    hydrationError,
    recordPageCount,
    selectDocument,
    setState,
  } = browse;

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
      title: activeItem?.title ?? 'Document',
      videoClassName: activeMediaKind === 'video' ? `document-video-${state.videoFit}` : undefined,
    };
  }, [activeItem?.title, activeMedia, activeMediaKind, activeRow, sheet, state.videoFit]);
  const readingPriority = priorityReadingRowId !== null && activeRowId === priorityReadingRowId;
  useEffect(() => {
    if (!readingPriority || !activeItem?.title) return;
    onPriorityLabel?.(activeRowId!, activeItem.title);
  }, [activeItem?.title, activeRowId, onPriorityLabel, readingPriority]);

  if (list.loading && list.pages.length === 0) {
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
    <MenuPop ref={popoverRef} style={style} sizing="anchored" className="document-options" role="dialog" data-testid="document-options" aria-label="View options">
      {sources.length > 1 && (
        <FormField label="Source" layout="inline">

          <PanelSelect
            className="row-height-select"
            data-testid="document-option-source"
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
        </FormField>
      )}
      <FormField label="Title override" layout="inline">
        {/* This is a per-view OVERRIDE of the sheet's default title column
            (set sheet-wide from the column '...' menu's "Use as row title")
            — the blank option restores that default instead of pinning a
            column forever. */}
        <PanelSelect
          className="row-height-select"
          data-testid="document-option-title"
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
      </FormField>
      {/* Layout / Fit are PDF page-rendering options; a text source has no
          pages, so offering them would be two controls that do nothing. List
          sort and Sync below are source-independent and stay. */}
      {source?.kind !== 'text' && (
        <>
      <FormField label="Layout" layout="inline">

        <PanelSelect
          className="row-height-select"
          data-testid="document-option-layout"
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
      </FormField>
      <FormField label="Fit" layout="inline">

        <PanelSelect
          className="row-height-select"
          data-testid="document-option-fit"
            menuRef={optionFitMenuRef}
          value={state.fit}
          onChange={(event) => setState({ fit: event.target.value as DocumentViewState['fit'] })}
        >
          <option value="width">Fit width</option>
          <option value="page">Fit page</option>
        </PanelSelect>
      </FormField>
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
      <FormField label="List sort" layout="inline">

        <PanelSelect
          className="row-height-select"
          data-testid="document-option-listsort"
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
      </FormField>
    </MenuPop>
  );

  return (
    <div
      className="document-view"
      data-testid="document-view"
      data-project-id={projectId}
      data-source-column-id={sourceColumn ? String(sourceColumn.id) : ''}
    >
      <DocumentList browse={browse} activeRowId={activeRowId} onOpen={onOpenDetail}
        onSelect={item => {
          selectDocument(item.rowId);
          if ((item.sourceKind === 'pdf' || item.sourceKind === 'image') && sourceColumn) {
            onRequestDocumentPriority?.(item.rowId, String(sourceColumn.id));
          }
        }}
        describe={item => {
          const rowId = item.rowId;
          const count = pageCounts[rowId];
          const characterCount = item.sourceKind === 'annotated_text'
            ? item.characterCount ?? 0
            : item.characterCount;
          const secondary = characterCount !== null
            ? characterCount > 0
              ? `${characterCount.toLocaleString()} characters`
              : 'Empty'
            : !item.sourcePresent
              ? 'No document'
              : count != null
                ? `${count} page${count === 1 ? '' : 's'}`
                : item.sourceLabel
                  ? mediaFilename({ url: item.sourceLabel, label: item.sourceLabel })
                    ?? item.sourceKind.toUpperCase()
                  : 'No document';
          return { secondary: readingPriority && rowId === activeRowId
            ? <span data-testid="document-reading-priority" role="status">Reading text now</span>
            : secondary };
        }} />
      {source?.kind === 'text' ? (
        <AnnotatedTextReader
          key={`text-reader-${activeRowId ?? 'none'}`}
          rowId={activeRowId}
          columnId={String(source.column.id)}
          title={activeItem?.title ?? 'No document selected'}
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
          alongsideTriggerRef={alongsideTriggerRef}
          onShowAlongside={alongside.open ? undefined : () => setAlongside({ ...alongside, open: true })}
          optionsOpen={optionsOpen}
          onToggleOptions={() => setOptionsOpen((open) => !open)}
          optionsPopover={optionsPopover}
          optionsMenuRefs={optionMenuRefs}
          selectionCount={0}
          onReplayLayer={onReplayAnnotationLayer ?? null}
        />
      ) : hydrationLoading ? (
        <section className="document-reader" data-testid="document-reader">
          <PanelLoading className="main-view-loading" label="Loading document…" />
        </section>
      ) : hydrationError ? (
        <section className="document-reader" data-testid="document-reader">
          <div className="document-list-message" role="alert">{hydrationError}</div>
        </section>
      ) : (
        <DocumentReader
          onSplitPdf={activeRowId && sourceColumn && onSplitPdf ? () => onSplitPdf(activeRowId, String(sourceColumn.id)) : undefined}
          key={`reader-${activeRowId ?? 'none'}-${state.readerLocationKey ?? ''}`}
          media={activeMedia}
          mediaKind={activeMediaKind}
          title={activeItem?.title ?? 'No document selected'}
          layout={state.layout}
          fit={state.fit}
          videoFit={state.videoFit}
          onVideoFitChange={(videoFit) => setState({ videoFit })}
          textLayer={state.textLayer}
          onPageCount={recordPageCount}
          initialPage={state.readerPage?.rowId === activeRowId
            ? state.readerPage.page
            : filePage(sourceColumn && activeRow
              ? activeRow.cells[String(sourceColumn.id)]
              : null)}
          initialTime={state.readerTime?.rowId === activeRowId
            ? state.readerTime.seconds
            : null}
          onFindSimilar={activeRowId && sourceColumn && onFindSimilar
            ? (target) => onFindSimilar({
                sheetId: sheet.id,
                rowId: activeRowId,
                columnId: String(sourceColumn.id),
                target,
              })
            : undefined}
          onPageChange={(page) => { if (activeRowId && (state.readerPage?.rowId !== activeRowId || state.readerPage.page !== page)) setState({ readerPage: { rowId: activeRowId, page } }); }}
          rowKey={activeRowId ?? 'none'}
          onOpenDetail={() => {
            if (activeRowId) onOpenDetail(activeRowId);
          }}
          canOpenDetail={Boolean(activeRowId)}
          alongsideTriggerRef={alongsideTriggerRef}
          onShowAlongside={alongside.open ? undefined : () => setAlongside({ ...alongside, open: true })}
          optionsOpen={optionsOpen}
          onToggleOptions={() => setOptionsOpen((open) => !open)}
          optionsPopover={optionsPopover}
          optionsMenuRefs={optionMenuRefs}
          selectionCount={0}
          timedTranscriptDocument={activeTimedTranscriptDocument}
        />
      )}
      {alongside.open && <DocumentAlongsidePane
        key={`${projectId}:${sheet.id}`}
        projectId={projectId}
        sheetId={sheet.id}
        columns={sheet.columns}
        rowId={activeRowId}
        hydrateRow={hydrateRow}
        selectedColumnId={alongside.columnId}
        onChangeColumn={(columnId) => setAlongside({ open: true, columnId })}
        onClose={() => setAlongside({ ...alongside, open: false })}
      />}
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
