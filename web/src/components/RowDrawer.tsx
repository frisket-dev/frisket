import { Check, Copy, FileSearch2, MapPin, Pause, Pencil, Play, RefreshCcw, X, Zap } from 'lucide-react';
import { useCallback, useEffect, useReducer, useRef, useState, type CSSProperties, type FocusEvent, type ReactNode, type RefObject } from 'react';
import { type CellEvidencePayload, type CellProvenance, type CellValue, type ColumnDef, type Row, type RunTraceRowEvidence, type SheetMeta } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useSelector } from '../bind/useSelector';
import { decodeEscapedText, looksLikeHtml } from '../displayText';
import {
  imageRegionsForRow,
  type RegionBox,
} from '../grid/cells';
import { presentationFor } from '../grid/typeRegistry';
import { previewColumnDef, previewValueRow } from '../grid/previewCells';
import type { PreviewCellDetail } from '../api/types';
import { resolveMediaValue } from '../media/resolveMediaValue';
import { projectBlobUrl } from '../api/raw/projectResources';
import { formatGeoPoint, parseGeoPointValue } from '../grid/geo';
import { coerceFiniteNumber, confClass, formatCell, formatDefaultNumberDisplay, formatUsdOrNone } from '../format';
import { formatUsd } from '../actions/model';
import {
  formatTimecode,
  isTemporalColumnType,
  parseTimelineValue,
  summarizeTemporalCell,
  type TemporalColumnType,
} from '../temporal/model';
import {
  replaceTimelineItemsFromDraft,
  timelineItemsDraftText,
  type EditableTimelineType,
} from '../temporal/draft';
import { HtmlView, MarkdownView } from '../markdown';
import { RowInspectorEvidenceSectionFrame } from '../workbench/contributions';
import { documentMediaColumns } from '../workbench/documentMedia';
import type { WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import { CitationChip } from './evidence/CitationChip';
import { useAnchoredPosition } from '../hooks/useAnchoredPosition';
import { useNativePopover } from '../hooks/useNativePopover';
import { createAudioPlaybackSource } from '../state/audioPlaybackStore';

const EMPTY_CONTRIBUTIONS: WorkbenchResolvedLayoutContribution[] = [];

function DetailContributionSlot({
  render,
  contribution,
}: {
  render: (c: WorkbenchResolvedLayoutContribution) => ReactNode;
  contribution: WorkbenchResolvedLayoutContribution;
}) {
  return <>{render(contribution)}</>;
}

export interface RowDrawerBodyProps {
  sheet: SheetMeta;
  row: Row;
  selectedColumnId?: string | null;
  onEdit(row: Row, col: ColumnDef, value: CellValue): Promise<void>;
  /** Resolved rowDetail contributions (plugin detail tabs) + the workspace
   *  dispatch that mounts them — the dock/sidebar pattern. First-party
   *  evidence stays per-cell inside the field renderer. */
  detailContributions?: WorkbenchResolvedLayoutContribution[];
  renderDetailContribution?(contribution: WorkbenchResolvedLayoutContribution): ReactNode;
  /** Scroll container for the field-into-view effect. The resident Detail
   *  column (InspectDetailColumn) owns the scroll frame; pass its ref so the
   *  selected-cell auto-scroll keeps working outside the retired overlay. */
  bodyRef?: RefObject<HTMLDivElement>;
  /** When the sheet has a media/file column, an "Open in Document view" entry
   *  point that jumps to
   *  the Document reader focused on this row + column. */
  onOpenInDocumentView?(columnId: string): void;
  /** Deliberate per-row retry of a terminal empty_output cell (the
   *  empty-output terminal-row contract): run.backfill with this row's exact
   *  id. The promise settles when the retry run finishes. */
  onRetryCell?(columnName: string): Promise<void>;
}

/** The Inspect content — lineage chips, per-field blocks, plugin detail
 *  contributions — WITHOUT any panel chrome. Hosted by the resident Detail
 *  column; the overlay Drawer wrapper is retired. */
export function RowDrawerBody({
  sheet,
  row,
  selectedColumnId,
  onEdit,
  detailContributions = EMPTY_CONTRIBUTIONS,
  renderDetailContribution,
  bodyRef,
  onOpenInDocumentView,
  onRetryCell,
}: RowDrawerBodyProps) {
  const fallbackRef = useRef<HTMLDivElement>(null);
  const drawerBodyRef = bodyRef ?? fallbackRef;
  const documentColumn = onOpenInDocumentView ? documentMediaColumns(sheet)[0] ?? null : null;

  useEffect(() => {
    if (!selectedColumnId || !drawerBodyRef.current) return;
    const el = drawerBodyRef.current.querySelector<HTMLElement>(
      `[data-row-field-id="${cssString(selectedColumnId)}"]`,
    );
    if (!el) return;
    scrollFieldIntoDrawerView(drawerBodyRef.current, el);
  }, [row.id, selectedColumnId, drawerBodyRef]);

  return (
    <>
      {documentColumn && onOpenInDocumentView && (
        <button
          type="button"
          className="row-lineage row-open-document"
          data-testid="row-open-in-document-view"
          title="Read this document at full size in the Document view"
          onClick={() => onOpenInDocumentView(String(documentColumn.id))}
        >
          ⤢ Open in Document view
        </button>
      )}
      {(row.childCount ?? 0) > 0 && (
        <button
          type="button"
          className="row-lineage"
          data-testid="row-children"
          onClick={() =>
            window.dispatchEvent(
              new CustomEvent('frisket:reveal-children', {
                detail: {
                  parentSheetId: sheet.id,
                  parentRowId: row.id,
                  parentRowIndex: row.index,
                  childCount: row.childCount,
                },
              }),
            )
          }
          title="Open the rows derived from this one"
        >
          ↳ {row.childCount} derived {row.childCount === 1 ? 'row' : 'rows'} →
        </button>
      )}
      {row.parentRowId && sheet.parent && (
        <button
          type="button"
          className="row-lineage"
          data-testid="row-lineage"
          onClick={() =>
            window.dispatchEvent(
              new CustomEvent('frisket:reveal-row', {
                detail: { sheetId: sheet.parent!.sheetId, rowId: row.parentRowId },
              }),
            )
          }
          title={`Open the source row in ${sheet.parent.sheetName}`}
        >
          ↰ derived from <strong>{sheet.parent.sheetName}</strong> via {sheet.parent.viaAction}
        </button>
      )}
      {sheet.columns.map((col) => (
        <RowField
          key={`${row.id}:${col.id}`}
          col={col}
          columns={sheet.columns}
          row={row}
          sheetId={sheet.id}
          selected={col.id === selectedColumnId}
          onEdit={onEdit}
          onRetryCell={onRetryCell}
        />
      ))}
      {renderDetailContribution &&
        detailContributions
          .flatMap((contribution) => {
            // Plugin contributions only: the first-party evidence section is
            // per-cell content inside the field renderer.
            if (
              contribution.runtimeSource !== 'runtimeIndex' ||
              (contribution.status !== 'enabled' && contribution.status !== 'disabled')
            ) return [];
            return [(
              <div
                key={`${contribution.contributionId}:${contribution.placementId}`}
                className="row-detail-contribution"
                data-testid={`row-detail-contribution-${contribution.contributionId.replace(/[^a-zA-Z0-9]+/g, '-')}`}
                data-contribution-id={contribution.contributionId}
                data-status={contribution.status}
                data-runtime-source={contribution.runtimeSource}
              >
                <DetailContributionSlot render={renderDetailContribution} contribution={contribution} />
              </div>
            )];
          })}
    </>
  );
}

function cssString(value: string): string {
  return value.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

function scrollFieldIntoDrawerView(drawerBody: HTMLElement, field: HTMLElement): void {
  const bodyRect = drawerBody.getBoundingClientRect();
  const fieldRect = field.getBoundingClientRect();
  const centeredTop =
    drawerBody.scrollTop +
    fieldRect.top -
    bodyRect.top -
    Math.max(0, drawerBody.clientHeight - fieldRect.height) / 2;
  const maxTop = Math.max(0, drawerBody.scrollHeight - drawerBody.clientHeight);
  const top = Math.min(maxTop, Math.max(0, centeredTop));
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  drawerBody.scrollTo({ top, behavior: reduceMotion ? 'auto' : 'smooth' });
}

function draftToValue(col: ColumnDef, draft: string): CellValue {
  const presentation = presentationFor(col.type);
  if (
    col.type === 'number' ||
    col.type === 'integer' ||
    presentation.base === 'number' ||
    presentation.renderer === 'stars'
  ) {
    const trimmed = draft.trim();
    return trimmed === '' ? null : Number(trimmed);
  }
  if (col.type === 'boolean') return draft === 'true';
  return draft;
}

type OptimisticCellValue = { source: CellValue; value: CellValue } | null;

type RowFieldState = {
  optimistic: OptimisticCellValue;
  editing: boolean;
  hovered: boolean;
  focused: boolean;
  draft: string;
};

type RowFieldAction =
  | { type: 'hovered'; hovered: boolean }
  | { type: 'focused'; focused: boolean }
  | { type: 'start-edit'; draft: string }
  | { type: 'cancel-edit' }
  | { type: 'draft'; draft: string }
  | { type: 'saved'; source: CellValue; value: CellValue };

function rowFieldInitialState(value: CellValue): RowFieldState {
  return {
    optimistic: null,
    editing: false,
    hovered: false,
    focused: false,
    draft: value === null ? '' : String(value),
  };
}

function rowFieldReducer(state: RowFieldState, action: RowFieldAction): RowFieldState {
  switch (action.type) {
    case 'hovered':
      return { ...state, hovered: action.hovered };
    case 'focused':
      return { ...state, focused: action.focused };
    case 'start-edit':
      return {
        ...state,
        editing: true,
        draft: action.draft,
      };
    case 'cancel-edit':
      return { ...state, editing: false };
    case 'draft':
      return { ...state, draft: action.draft };
    case 'saved':
      return {
        ...state,
        optimistic: { source: action.source, value: action.value },
        editing: false,
      };
  }
}

export function RowField({
  col,
  columns,
  row,
  sheetId = '',
  selected,
  onEdit,
  onRetryCell,
}: {
  col: ColumnDef;
  columns: ColumnDef[];
  row: Row;
  sheetId?: string;
  selected: boolean;
  onEdit(row: Row, col: ColumnDef, value: CellValue): Promise<void>;
  onRetryCell?(columnName: string): Promise<void>;
}) {
  const value = row.cells?.[col.id] ?? null;
  const [fieldState, dispatch] = useReducer(rowFieldReducer, value, rowFieldInitialState);
  const { optimistic, editing, hovered, focused, draft } = fieldState;
  const displayValue = optimistic && Object.is(optimistic.source, value) ? optimistic.value : value;
  const editableTimelineType: EditableTimelineType | null = isTemporalColumnType(col.type)
    ? col.type
    : null;
  const temporalDraftText = editableTimelineType
    ? timelineItemsDraftText(displayValue, editableTimelineType)
    : null;
  const temporalDraftResult = editableTimelineType
    ? replaceTimelineItemsFromDraft(displayValue, editableTimelineType, draft)
    : null;
  const canEdit = editableTimelineType
    ? temporalDraftText !== null
    : !col.ai && !isTemporalColumnType(col.type);
  const copyText = editableTimelineType
    ? temporalDraftText ?? ''
    : isTemporalColumnType(col.type)
      ? summarizeTemporalCell(displayValue, col.type).copyText
      : displayValue == null ? '' : String(displayValue);
  const prov = row.provenance?.[col.id] ?? null;
  const state = row.cellStates?.[col.id] ?? null;
  // Terminal-failure retry (the empty-output terminal-row contract): the run
  // failed this row honestly and automation never revisits it; only a
  // deliberate user click re-runs it. Busy until the retry run finishes —
  // afterBackfill then refreshes the row, so the block clears itself on
  // success and stays (honestly) on a repeat empty result.
  const [retryBusy, setRetryBusy] = useState(false);
  const retryable = Boolean(
    onRetryCell && col.ai && row.cellOutcomes?.[col.id] === 'empty_output',
  );
  const runId = prov?.currentValueRef?.kind === 'run_result'
    ? prov.currentValueRef.runId
    : null;
  const actionsVisible = hovered || focused || editing;
  const onFieldBlur = (event: FocusEvent<HTMLElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      dispatch({ type: 'focused', focused: false });
    }
  };
  return (
    <section
      className={`row-field${selected ? ' row-field-selected' : ''}`}
      data-testid={`row-field-${col.name}`}
      data-row-field-id={col.id}
      data-selected-cell={selected ? 'true' : undefined}
      aria-label={`${col.name} cell`}
      onMouseEnter={() => dispatch({ type: 'hovered', hovered: true })}
      onMouseLeave={() => dispatch({ type: 'hovered', hovered: false })}
      onFocus={() => dispatch({ type: 'focused', focused: true })}
      onBlur={onFieldBlur}
    >
      <header className="row-field-name">
        {col.ai && <Zap size={11} className="ai-bolt" />}
        {col.name}
        <span className="row-field-type">{col.type}</span>
        {state && (
          <span className={`row-field-state row-field-state-${state}`} data-testid={`cell-state-${col.name}`}>
            {state}
          </span>
        )}
        <div className="row-field-actions">
          <span className="row-field-action-slot">
            {actionsVisible && col.ai && runId ? (
              <ExplainBlock
                key={`${runId}:${row.id}:${prov?.currentValueRef?.columnId ?? col.id}`}
                runId={runId}
                rowId={row.id}
                columnId={prov?.currentValueRef?.columnId ?? col.id}
                compact
                columnName={col.name}
                testId="cell-action-explain"
              />
            ) : (
              <span className="row-field-action-placeholder" aria-hidden="true" />
            )}
          </span>
          <span className="row-field-action-slot">
            {actionsVisible && canEdit ? (
              <button
                type="button"
                className="row-field-icon"
                data-testid={`cell-edit-${col.name}`}
                aria-label={editing ? `Cancel editing ${col.name}` : `Edit ${col.name}`}
                title={`Edit ${col.name}`}
                onClick={() => {
                  if (editing) {
                    dispatch({ type: 'cancel-edit' });
                    return;
                  }
                  dispatch({
                    type: 'start-edit',
                    draft: editableTimelineType
                      ? temporalDraftText ?? ''
                      : displayValue === null ? '' : String(displayValue),
                  });
                }}
              >
                {editing ? <X size={12} /> : <Pencil size={12} />}
              </button>
            ) : (
              <span className="row-field-action-placeholder" aria-hidden="true" />
            )}
          </span>
          <span className="row-field-action-slot">
            {actionsVisible ? (
              <button
                type="button"
                className="row-field-icon"
                data-testid="cell-action-copy"
                aria-label={`Copy ${col.name} cell`}
                title={`Copy ${col.name}`}
                onClick={() => void navigator.clipboard.writeText(copyText)}
              >
                <Copy size={12} />
              </button>
            ) : (
              <span className="row-field-action-placeholder" aria-hidden="true" />
            )}
          </span>
        </div>
      </header>
      {editing ? (
        <form
          className="row-field-editor"
          onSubmit={(e) => {
            e.preventDefault();
            const next = editableTimelineType
              ? temporalDraftResult?.ok ? temporalDraftResult.value : null
              : draftToValue(col, draft);
            if (next === null && editableTimelineType) return;
            void onEdit(row, col, next).then(() => {
              dispatch({ type: 'saved', source: value, value: next });
            }, () => undefined);
          }}
        >
          <textarea
            className="form-input row-field-input"
            data-testid={`cell-editor-${col.name}`}
            aria-label={`Edit ${col.name} value`}
            value={draft}
            rows={3}
            placeholder={editableTimelineType === 'timeline_points'
              ? '00:10,Optional label'
              : editableTimelineType === 'timeline_point'
                ? '00:10,Optional label'
              : editableTimelineType === 'timeline_ranges'
                ? '00:10,00:30,Optional label'
                : editableTimelineType === 'timeline_range'
                  ? '00:10,00:30,Optional label'
                : undefined}
            aria-invalid={Boolean(temporalDraftResult && !temporalDraftResult.ok)}
            onChange={(e) => dispatch({ type: 'draft', draft: e.target.value })}
          />
          {temporalDraftResult && !temporalDraftResult.ok && (
            <p className="temporal-validation-error" role="alert" data-testid={`cell-editor-error-${col.name}`}>
              {temporalDraftResult.error}
            </p>
          )}
          <button
            type="submit"
            className="mini-btn"
            data-testid={`cell-save-${col.name}`}
            disabled={Boolean(temporalDraftResult && !temporalDraftResult.ok)}
          >
            <Check size={12} /> Save
          </button>
        </form>
      ) : (
        <ClampedField>
          <FieldValue
            col={col}
            columns={columns}
            row={row}
            sheetId={sheetId}
            value={displayValue}
          />
        </ClampedField>
      )}
      {col.ai && prov && <ProvenanceBlock prov={prov} />}
      {retryable && onRetryCell && (
        <div className="row-field-retry" data-testid={`cell-retry-${col.name}`}>
          <span className="row-field-retry-note">
            The engine returned empty output for this row — retrying may well
            do the same.
          </span>
          <button
            type="button"
            className="mini-btn"
            data-testid={`cell-retry-button-${col.name}`}
            disabled={retryBusy}
            onClick={() => {
              setRetryBusy(true);
              void onRetryCell(col.name).finally(() => setRetryBusy(false));
            }}
          >
            <RefreshCcw size={11} /> {retryBusy ? 'Retrying…' : 'Retry anyway'}
          </button>
        </div>
      )}
      <RowInspectorEvidenceSectionFrame>
        <CellEvidenceBlock
          rowId={row.id}
          columnId={col.id}
          columnName={col.name}
        />
      </RowInspectorEvidenceSectionFrame>
    </section>
  );
}

type EvidenceBlockState =
  | { phase: 'loading' | 'empty' | 'error'; payload?: null }
  | { phase: 'ready'; payload: CellEvidencePayload; auditOpen: boolean };

function CellEvidenceBlock({
  rowId,
  columnId,
  columnName,
}: {
  rowId: string;
  columnId: string;
  columnName: string;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const [state, setState] = useState<EvidenceBlockState>({ phase: 'loading' });

  useEffect(() => {
    let alive = true;
    api
      .getCellEvidence(rowId, columnId)
      .then((payload) => {
        if (!alive) return;
        if (payload.links.length === 0 && payload.stale_count === 0) {
          setState({ phase: 'empty' });
        } else {
          setState({ phase: 'ready', payload, auditOpen: false });
        }
      })
      .catch(() => {
        if (alive) setState({ phase: 'error' });
      });
    return () => {
      alive = false;
    };
  }, [rowId, columnId]);

  if (state.phase === 'error') {
    return (
      <div className="cell-evidence cell-evidence-error" data-testid={`cell-evidence-error-${columnName}`}>
        Evidence unavailable
      </div>
    );
  }
  if (state.phase !== 'ready') return null;

  const openAudit = () => {
    if (state.auditOpen) {
      setState((current) => (
        current.phase === 'ready' ? { ...current, auditOpen: false } : current
      ));
      return;
    }
    api
      .getCellEvidence(rowId, columnId, { includeStale: true })
      .then((payload) => setState({ phase: 'ready', payload, auditOpen: true }))
      .catch(() => setState((current) => (
        current.phase === 'ready' ? { ...current, auditOpen: true } : current
      )));
  };
  const activeLinks = state.payload.links.filter((link) => link.status === 'active');
  const staleLinks = state.payload.links.filter((link) => link.status === 'stale');
  return (
    <div className="cell-evidence" data-testid={`cell-evidence-${columnName}`}>
      {activeLinks.length > 0 && (
        <div className="cell-evidence-links" data-testid={`cell-evidence-active-${columnName}`}>
          {activeLinks.map((link) => (
            <CitationChip
              key={link.stable_id}
              linkId={link.stable_id}
              label="Evidence"
              snippet={link.snippet}
              testId={`cell-evidence-open-${columnName}`}
            />
          ))}
        </div>
      )}
      {state.payload.stale_count > 0 && (
        <button
          type="button"
          className="mini-btn cell-evidence-audit-toggle"
          data-testid={`cell-evidence-stale-toggle-${columnName}`}
          onClick={openAudit}
        >
          stale evidence ({state.payload.stale_count})
        </button>
      )}
      {state.auditOpen && staleLinks.length > 0 && (
        <div className="cell-evidence-stale-list" data-testid={`cell-evidence-stale-${columnName}`}>
          {staleLinks.map((link) => (
            <CitationChip
              key={link.stable_id}
              linkId={link.stable_id}
              label="Stale evidence"
              snippet={link.snippet}
              className="cell-evidence-link-stale"
              testId={`cell-evidence-open-stale-${columnName}`}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** Every detail field's value is capped at half the viewport height so a long
 *  transcript, entity table, or text blob can't push the rest of the Inspect
 *  column off-screen. When the value overflows the cap, a subtle "Show more"
 *  reveals the rest in place (and "Show less" re-collapses it). Content that
 *  fits shows no toggle at all. */
export function ClampedField({ children }: { children: ReactNode }) {
  const contentRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);

  // Measure only while collapsed: expanding removes the max-height so
  // scrollHeight would equal clientHeight and falsely read as "fits". Keeping
  // `overflowing` latched true while expanded is what keeps "Show less" shown.
  useEffect(() => {
    if (expanded) return;
    const el = contentRef.current;
    if (!el) return;
    const measure = () => setOverflowing(el.scrollHeight - el.clientHeight > 1);
    measure();
    // jsdom (unit tests) has no ResizeObserver and no layout, so the one-shot
    // measure above is enough there; guard so the component still mounts.
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [expanded]);

  return (
    <div className="row-field-clamp" data-clamped={!expanded && overflowing ? 'true' : undefined}>
      <div
        ref={contentRef}
        className={`row-field-clamp-content${expanded ? ' row-field-clamp-expanded' : ''}`}
        data-testid="row-field-clamp"
      >
        {children}
      </div>
      {overflowing && (
        <button
          type="button"
          className="mini-btn row-field-clamp-toggle"
          data-testid="row-field-clamp-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  );
}

/** A sample is deliberately not a RowField: no edit, retry or stored provenance. */
export function PreviewFieldValue({ preview, showHeading = true }: {
  preview: PreviewCellDetail; showHeading?: boolean;
}) {
  const col = previewColumnDef(preview.column);
  return (
    <section className="row-field" data-testid="preview-field-value">
      {showHeading && <h3>{preview.column.name} · Preview</h3>}
      {preview.cell.error ? (
        <div className="inspect-detail-preview-error" data-testid="inspect-detail-preview-error" role="alert">
          <strong>Preview error</strong>
          <p>{preview.cell.error}</p>
        </div>
      ) : (
        <ClampedField>
          <FieldValue col={col} columns={[col]} row={previewValueRow(col, preview.cell)} value={preview.cell.value} />
        </ClampedField>
      )}
    </section>
  );
}

export function FieldValue({
  col,
  columns,
  row,
  sheetId = '',
  value,
}: {
  col: ColumnDef;
  columns: ColumnDef[];
  row: Row;
  sheetId?: string;
  value: Row['cells'][string];
}) {
  const { chromePreferences: { projectId } } = useWorkspaceStores();
  // Explicit "(empty)" for a real null/empty value so the detail panel never
  // shows blank (or, pre-coercion, the literal string "null") as if it were
  // text. Styled by .row-field-empty (dim, italic, --text-light).
  if (value === null || value === '') return <div className="row-field-empty">(empty)</div>;
  // Imported email bodies explicitly mark HTML-looking source as literal text.
  // This must precede every rich/text enhancement: Markdown, HTML detection,
  // and URL linkification can each create elements or fetchable resources.
  if (col.format === 'plain_text') {
    return <div className="row-field-value">{String(value)}</div>;
  }
  const presentation = presentationFor(col.type);
  if (presentation.renderer === 'stars') {
    return <StarsValue value={value} />;
  }
  if (col.format === 'markdown') {
    return (
      <MarkdownView
        className="row-field-value row-field-markdown"
        source={String(value)}
        testId="markdown-value"
        decodeEscapes={Boolean(col.ai)}
      />
    );
  }
  if (isTemporalColumnType(col.type)) {
    return <TemporalValue type={col.type} value={value} />;
  }
  switch (col.type) {
    case 'image': {
      const media = resolveMediaValue(value, projectId);
      if (!media) return <div className="row-field-empty">empty</div>;
      const regions = imageRegionsForRow(row, col);
      if (regions.length === 0) {
        return <img className="row-field-media" src={media.url} alt={col.name} loading="lazy" />;
      }
      return <RegionImageValue url={media.url} alt={col.name} regions={regions} />;
    }
    case 'video': {
      const media = resolveMediaValue(value, projectId);
      if (!media) return <div className="row-field-empty">empty</div>;
      const captionTrack = mediaCaptionTrack(row, columns, col);
      return (
        <>
          <video
            className="row-field-media"
            src={media.url}
            controls
            preload="metadata"
            aria-label={`${col.name} video: ${media.label}`}
          >
            <track
              kind="captions"
              src={captionTrack.src}
              srcLang="en"
              label={captionTrack.label}
              default
            />
          </video>
          <div className="row-field-media-label">{media.label}</div>
        </>
      );
    }
    case 'audio': {
      const media = resolveMediaValue(value, projectId);
      if (!media) return <div className="row-field-empty">empty</div>;
      return (
        <AudioFieldControl
          columnId={col.id}
          columnName={col.name}
          label={media.label}
          rowId={row.id}
          sheetId={sheetId}
          url={media.url}
        />
      );
    }
    case 'file': {
      const media = resolveMediaValue(value, projectId);
      if (!media) return <div className="row-field-empty">empty</div>;
      return (
        <a className="row-field-link" href={media.url} target="_blank" rel="noopener noreferrer">
          {media.label}
        </a>
      );
    }
    case 'link':
      return <LinkValue value={String(value)} />;
    case 'geo_point':
      return <GeoPointValue value={value} />;
    case 'boolean':
      return <div className="row-field-value">{value ? 'true' : 'false'}</div>;
    case 'json':
      return <JsonValue value={value} isEmailAttachments={col.name === 'attachments'} />;
  }

  if (col.type === 'number' || col.type === 'integer') {
    const n = coerceFiniteNumber(value);
    if (n !== null) {
      const formattedNumber =
        formatCell(n, col.format) ?? formatDefaultNumberDisplay(n, col.type);
      return <div className="row-field-value">{formattedNumber}</div>;
    }
  }

  const formatted = formatCell(value, col.format);
  if (formatted) return <div className="row-field-value">{formatted}</div>;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
      return <JsonValue value={value} />;
    }
    const rendered = col.ai ? decodeEscapedText(value) : value;
    if (looksLikeHtml(value) || looksLikeHtml(rendered)) {
      return <HtmlValue raw={value} source={rendered} />;
    }
    return <div className="row-field-value">{linkify(rendered)}</div>;
  }
  return <div className="row-field-value">{linkify(String(value))}</div>;
}

function AudioFieldControl({
  columnId,
  columnName,
  label,
  rowId,
  sheetId,
  url,
}: {
  columnId: string;
  columnName: string;
  label: string;
  rowId: string;
  sheetId: string;
  url: string;
}) {
  const { audioPlayback } = useWorkspaceStores();
  const source = createAudioPlaybackSource({
    url,
    label,
    sheetId,
    rowId,
    columnId,
  });
  const playback = useSelector(audioPlayback.store, (state) => (
    state.source?.key === source.key && state.playing ? 'playing' : 'paused'
  ));

  return (
    <button
      type="button"
      className="row-field-audio-control"
      data-testid="row-audio"
      aria-label={`${playback === 'playing' ? 'Pause' : 'Play'} ${columnName} audio: ${label}`}
      onClick={() => audioPlayback.toggle(source)}
    >
      {playback === 'playing' ? <Pause size={14} aria-hidden /> : <Play size={14} aria-hidden />}
      <span>{label}</span>
    </button>
  );
}

function TemporalValue({
  type,
  value,
}: {
  type: TemporalColumnType;
  value: CellValue;
}) {
  const parsed = parseTimelineValue(value, type);
  if (!parsed) {
    return (
      <div className="row-field-temporal-invalid" role="status" data-testid="temporal-value-invalid">
        Invalid temporal value
      </div>
    );
  }
  const points = parsed.schema_version === 'frisket.timeline_point.v1'
    ? [parsed.item]
    : parsed.schema_version === 'frisket.timeline_points.v1'
      ? parsed.items
      : [];
  const ranges = parsed.schema_version === 'frisket.timeline_range.v1'
    ? [parsed.item]
    : parsed.schema_version === 'frisket.timeline_ranges.v1'
      ? parsed.items
      : [];
  return (
    <div className="row-field-temporal" data-testid="temporal-value">
      <div className="row-field-temporal-anchor">
        <span title={parsed.timeline.artifact_stable_id}>Source timeline</span>
        {parsed.timeline.duration_ms != null && (
          <span>{formatTimecode(parsed.timeline.duration_ms)} total</span>
        )}
      </div>
      {points.length === 0 && ranges.length === 0 ? (
        <div className="row-field-empty">No temporal selections</div>
      ) : (
        <ol className="row-field-temporal-items">
          {points.map((point) => (
            <li key={point.id} title={point.id}>
              <strong>{formatTimecode(point.at_ms)}</strong>
              {point.label ? <span>{point.label}</span> : null}
            </li>
          ))}
          {ranges.map((range) => (
            <li key={range.id} title={range.id}>
              <strong>{formatTimecode(range.start_ms)} – {formatTimecode(range.end_ms)}</strong>
              {range.label ? <span>{range.label}</span> : null}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// Exported as the render seam for StarsValue.test.tsx; plugin lifecycle and
// value validation remain browser-covered separately.
export function StarsValue({ value }: { value: unknown }) {
  const n = coerceFiniteNumber(value);
  if (n === null) return <div className="row-field-value">{String(value)}</div>;
  const clamped = Math.max(0, Math.min(5, n));
  const whole = Math.floor(clamped);
  const half = clamped - whole >= 0.5 && whole < 5;
  const stars = `${'★'.repeat(whole)}${half ? '½' : ''}${'☆'.repeat(5 - whole - (half ? 1 : 0))}`;
  return (
    <div className="row-field-value" data-testid="stars-value">
      <span aria-label={`${n} out of 5`}>{stars}</span>{' '}
      <span data-testid="stars-raw-value">{formatDefaultNumberDisplay(n, 'number')}</span>
    </div>
  );
}

function RegionImageValue({
  url,
  alt,
  regions,
}: {
  url: string;
  alt: string;
  regions: RegionBox[];
}) {
  const resizeObserverRef = useRef<ResizeObserver | null>(null);
  const [imageSize, setImageSize] = useState<{ naturalWidth: number; naturalHeight: number } | null>(null);
  const [frameSize, setFrameSize] = useState<{ width: number; height: number } | null>(null);

  const setFrameRef = useCallback((frame: HTMLDivElement | null) => {
    resizeObserverRef.current?.disconnect();
    resizeObserverRef.current = null;
    if (!frame) return;
    const updateSize = () => {
      const rect = frame.getBoundingClientRect();
      setFrameSize({ width: rect.width, height: rect.height });
    };
    updateSize();
    const observer = new ResizeObserver(updateSize);
    observer.observe(frame);
    resizeObserverRef.current = observer;
  }, []);

  const imageRect = imageSize && frameSize
    ? containedImageRect(frameSize.width, frameSize.height, imageSize.naturalWidth, imageSize.naturalHeight)
    : null;

  return (
    <div className="row-field-region-media" data-testid="row-region-image" ref={setFrameRef}>
      <img
        className="row-field-media"
        src={url}
        alt={alt}
        loading="lazy"
        onLoad={(event) => {
          const img = event.currentTarget;
          setImageSize({ naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight });
        }}
      />
      {imageRect && regions.map((region, index) => (
        <span
          key={`${region.x}:${region.y}:${region.w}:${region.h}:${index}`}
          className="row-field-region-box"
          data-testid="row-region-box"
          title={region.label}
          style={regionBoxStyle(region, imageRect)}
        />
      ))}
    </div>
  );
}

function containedImageRect(
  frameWidth: number,
  frameHeight: number,
  naturalWidth: number,
  naturalHeight: number,
): { left: number; top: number; scale: number } {
  if (frameWidth <= 0 || frameHeight <= 0 || naturalWidth <= 0 || naturalHeight <= 0) {
    return { left: 0, top: 0, scale: 0 };
  }
  const scale = Math.min(frameWidth / naturalWidth, frameHeight / naturalHeight);
  return {
    left: (frameWidth - naturalWidth * scale) / 2,
    top: (frameHeight - naturalHeight * scale) / 2,
    scale,
  };
}

function regionBoxStyle(
  region: RegionBox,
  imageRect: { left: number; top: number; scale: number },
): CSSProperties {
  return {
    left: `${imageRect.left + region.x * imageRect.scale}px`,
    top: `${imageRect.top + region.y * imageRect.scale}px`,
    width: `${region.w * imageRect.scale}px`,
    height: `${region.h * imageRect.scale}px`,
  };
}

type CaptionTrack = {
  src: string;
  label: string;
};

const CAPTION_COLUMN_TERMS = ['transcript', 'caption', 'captions', 'subtitle', 'subtitles'];
const MAX_CAPTION_TRACK_CHARS = 40000;

function mediaCaptionTrack(row: Row, columns: ColumnDef[], mediaCol: ColumnDef): CaptionTrack {
  let best: { score: number; columnName: string; text: string } | null = null;
  for (const candidate of columns) {
    if (candidate.id === mediaCol.id || candidate.type !== 'text') continue;
    const score = captionColumnScore(candidate.name, mediaCol);
    if (score === null) continue;
    const text = captionTrackText(row.cells[candidate.id]);
    if (!text) continue;
    if (!best || score < best.score) {
      best = { score, columnName: candidate.name, text };
    }
  }
  if (!best) {
    return {
      src: webVttDataUrl(`No transcript or caption text is available for ${mediaCol.name}.`),
      label: 'No captions available',
    };
  }
  return {
    src: webVttDataUrl(best.text),
    label: best.columnName,
  };
}

function captionColumnScore(columnName: string, mediaCol: ColumnDef): number | null {
  const normalizedName = normalizeCaptionColumnName(columnName);
  if (!CAPTION_COLUMN_TERMS.some((term) => normalizedName.split(' ').includes(term))) {
    return null;
  }

  const normalizedMediaName = normalizeCaptionColumnName(mediaCol.name);
  if (normalizedMediaName && normalizedName.includes(normalizedMediaName)) return 0;
  if (normalizedName === 'transcript') return 1;
  if (
    mediaCol.type === 'video' &&
    ['caption', 'captions', 'subtitle', 'subtitles'].includes(normalizedName)
  ) {
    return 2;
  }
  if (normalizedName.includes('transcript')) return 3;
  return null;
}

function normalizeCaptionColumnName(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

function captionTrackText(value: CellValue | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) return null;
  return trimmed.slice(0, MAX_CAPTION_TRACK_CHARS);
}

function webVttDataUrl(text: string): string {
  const cueText = text.replace(/\r\n?/g, '\n').replace(/-->/g, '->').trim();
  const vtt = `WEBVTT\n\n00:00:00.000 --> 99:59:59.000\n${cueText}\n`;
  return `data:text/vtt;charset=utf-8,${encodeURIComponent(vtt)}`;
}

function HtmlValue({ raw, source }: { raw: string; source: string }) {
  return (
    <div className="row-field-rich-text">
      <HtmlView className="row-field-value row-field-html" source={source} testId="html-value" />
      <details className="row-field-raw">
        <summary>Raw</summary>
        <pre>{raw}</pre>
      </details>
    </div>
  );
}

function GeoPointValue({ value }: { value: CellValue }) {
  const point = parseGeoPointValue(value);
  if (!point) return <div className="row-field-value">{String(value)}</div>;
  const href =
    `https://www.openstreetmap.org/?mlat=${point.lat}&mlon=${point.lon}` +
    `#map=14/${point.lat}/${point.lon}`;
  return (
    <div className="geo-point-detail" data-testid="geo-point-value">
      <div className="geo-point-coords" data-testid="geo-point-coordinates">
        <MapPin size={15} />
        <span>{formatGeoPoint(point)}</span>
      </div>
      <a
        className="row-field-link"
        data-testid="geo-point-map-link"
        href={href}
        target="_blank"
        rel="noopener noreferrer"
      >
        Open map
      </a>
    </div>
  );
}

const URL_RE = /https?:\/\/[^\s"'<>)\]}]+/g;

const isUrl = (s: string): boolean => /^https?:\/\//.test(s);

function safeHttpUrl(raw: string): string | null {
  const url = raw.trim();
  if (!/^https?:\/\//i.test(url)) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? url : null;
  } catch {
    return null;
  }
}

function LinkValue({ value }: { value: string }) {
  const href = safeHttpUrl(value);
  if (!href) return <div className="row-field-value">{value}</div>;
  return (
    <a className="row-field-link" href={href} target="_blank" rel="noopener noreferrer">
      {value}
    </a>
  );
}

/** Split text on URLs and wrap each in an anchor. */
function linkify(text: string): ReactNode {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(URL_RE)) {
    const offset = m.index ?? 0;
    if (offset > last) parts.push(text.slice(last, offset));
    const href = safeHttpUrl(m[0]);
    parts.push(
      href ? (
        <a key={`${offset}:${m[0]}`} className="row-field-link" href={href} target="_blank" rel="noopener noreferrer">
          {m[0]}
        </a>
      ) : (
        m[0]
      ),
    );
    last = offset + m[0].length;
  }
  if (parts.length === 0) return text;
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

const cellText = (v: unknown): string =>
  v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);

function jsonKeyBase(value: unknown): string {
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    for (const field of ['id', 'url', 'href', 'link', 'name', 'title']) {
      const fieldValue = record[field];
      if (typeof fieldValue === 'string' || typeof fieldValue === 'number') {
        return `${field}:${String(fieldValue)}`;
      }
    }
  }
  return cellText(value);
}

function keyedJsonItems<T>(items: T[]): Array<{ item: T; key: string }> {
  const seen = new Map<string, number>();
  return items.map((item) => {
    const base = jsonKeyBase(item);
    const duplicateCount = seen.get(base) ?? 0;
    seen.set(base, duplicateCount + 1);
    return {
      item,
      key: duplicateCount === 0 ? base : `${base}#${duplicateCount + 1}`,
    };
  });
}

/** Frames retain their timestamp beside a nested image. Closed image shapes
 * keep other JSON records (including face geometry) in the ordinary table. */
interface ImageBlobEnvelope {
  blob?: string;
  inlineDataUrl?: string;
  omitted?: boolean;
  mime: string;
  filename?: string;
  t?: number;
}

function imageBlobEnvelope(item: unknown): ImageBlobEnvelope | null {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
  const rec = item as Record<string, unknown>;
  const frame = Object.keys(rec).length === 2 && 't' in rec && 'image' in rec;
  if (frame && (typeof rec.t !== 'number' || !Number.isFinite(rec.t))) return null;
  const value = frame ? rec.image : rec;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const image = value as Record<string, unknown>;
  if (typeof image.mime !== 'string' || !image.mime.startsWith('image/')) return null;
  const keys = Object.keys(image);
  const stored = typeof image.blob === 'string' && /^[0-9a-f]{64}$/.test(image.blob)
    && keys.every((key) => ['blob', 'mime', 'filename'].includes(key));
  const inline = image.mime === 'image/jpeg' && typeof image.inline_data_url === 'string'
    && /^data:image\/jpeg;base64,[A-Za-z0-9+/]+={0,2}$/.test(image.inline_data_url)
    && image.inline_data_url.length <= 2_796_228
    && keys.length === 3 && keys.every((key) => ['inline_data_url', 'mime', 'filename'].includes(key));
  const omitted = image.mime === 'image/jpeg' && image.preview_omitted === true
    && keys.length === 3 && keys.every((key) => ['preview_omitted', 'mime', 'filename'].includes(key));
  if (!stored && !inline && !omitted) return null;
  if ((inline || omitted) && typeof image.filename !== 'string') return null;
  return {
    blob: stored ? image.blob as string : undefined,
    inlineDataUrl: inline ? image.inline_data_url as string : undefined,
    omitted,
    mime: image.mime,
    filename: typeof image.filename === 'string' && image.filename ? image.filename : undefined,
    t: frame ? rec.t as number : undefined,
  };
}

interface DownloadBlobEnvelope {
  blob: string;
  mime: string;
  filename: string;
}

const BLOB_DIGEST_RE = /^[0-9a-f]{64}$/;

/** Email attachments are deliberately a much narrower shape than media
 * envelopes. Exact keys prevent a producer extension or mixed list from being
 * silently hidden by the filename-only download presentation. */
function downloadBlobEnvelope(item: unknown): DownloadBlobEnvelope | null {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
  const record = item as Record<string, unknown>;
  const keys = Object.keys(record);
  if (
    keys.length !== 3
    || !keys.includes('blob')
    || !keys.includes('mime')
    || !keys.includes('filename')
    || typeof record.blob !== 'string'
    || !BLOB_DIGEST_RE.test(record.blob)
    || typeof record.mime !== 'string'
    || typeof record.filename !== 'string'
  ) return null;
  return { blob: record.blob, mime: record.mime, filename: record.filename };
}

/** The json-mini-table primitive: an array of objects rendered as a compact
 *  table with linkified cells. Shared by RowDrawer's JSON cell view (6-key cap)
 *  and the PDF-tables picker (per-table_index groups, metadata keys hidden by
 *  default). `hiddenKeys` are dropped from the header/body unless `showHidden`;
 *  `maxKeys` caps the visible column count (null = no cap). A list that is
 *  entirely image-blob envelopes renders as a thumbnail strip instead. */
export function JsonMiniTable({
  items,
  hiddenKeys,
  showHidden = false,
  maxKeys = null,
  testId = 'json-mini-table',
  disableBlobThumbnails = false,
}: {
  items: Record<string, unknown>[];
  hiddenKeys?: ReadonlySet<string>;
  showHidden?: boolean;
  maxKeys?: number | null;
  testId?: string;
  /** Email attachment fallbacks retain their generic JSON table rather than
   * turning image-like malformed envelopes into resource-loading previews. */
  disableBlobThumbnails?: boolean;
}) {
  const { chromePreferences: { projectId } } = useWorkspaceStores();
  // A homogeneous list of image-blob envelopes (e.g. the `Extract frames`
  // `frames` column) becomes visible thumbnails rather than a table of blob
  // hashes. Any non-image item falls through to the text table below.
  const envelopes = items.map(imageBlobEnvelope);
  if (!disableBlobThumbnails && items.length > 0 && envelopes.every((e) => e !== null)) {
    return (
      <div className="json-blob-thumbs" data-testid="json-blob-thumbs">
        {envelopes.map((e, i) => {
          const env = e as ImageBlobEnvelope;
          const time = env.t != null ? formatTimecode(Math.round(env.t * 1000)) : null;
          const caption = env.filename ?? `${env.blob?.slice(0, 10)}…`;
          const alt = time ? `${caption} (${time})` : caption;
          return (
            <figure className="json-blob-thumb" key={`${env.blob}-${i}`}>
              {env.omitted ? <span className="muted" data-testid="json-blob-thumb-omitted">
                Preview image omitted (size or image-count limit).
              </span> : <img
                data-testid="json-blob-thumb"
                src={env.inlineDataUrl ?? projectBlobUrl(projectId, env.blob!)}
                alt={alt}
                title={alt}
                loading="lazy"
              />}
              <figcaption>
                {caption}
                {time ? <span className="json-blob-thumb-time"> {time}</span> : null}
              </figcaption>
            </figure>
          );
        })}
      </div>
    );
  }

  const keys: string[] = [];
  const seenKeys = new Set<string>();
  for (const it of items) {
    for (const k of Object.keys(it)) {
      if (!seenKeys.has(k)) {
        seenKeys.add(k);
        keys.push(k);
      }
    }
  }
  const visibleKeys =
    hiddenKeys && !showHidden ? keys.filter((k) => !hiddenKeys.has(k)) : keys;
  const shownKeys = typeof maxKeys === 'number' ? visibleKeys.slice(0, maxKeys) : visibleKeys;
  return (
    <div className="json-results" data-testid={testId}>
      <table className="json-table">
        <thead>
          <tr>{shownKeys.map((k) => <th key={k}>{k}</th>)}</tr>
        </thead>
        <tbody>
          {keyedJsonItems(items).map(({ item: it, key }) => (
            <tr key={key}>
              {shownKeys.map((k) => {
                const v = it[k];
                return (
                  <td key={k}>
                    {typeof v === 'string' && isUrl(v) ? (
                      <a className="row-field-link" href={v} target="_blank" rel="noopener noreferrer">
                        {v.replace(/^https?:\/\/(www\.)?/, '').slice(0, 60)}
                      </a>
                    ) : (
                      linkify(cellText(v))
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Entity-mention plumbing fields hidden from the detail table — only `text`
// and `type` read as content (frisket/ops/entities.py emits
// {text, type, start, end, score, fingerprint?, metadata?}).
const ENTITY_HIDDEN_KEYS: ReadonlySet<string> = new Set([
  'start',
  'end',
  'score',
  'fingerprint',
  'metadata',
]);

// Transcript segments ({segment_index, start, end, text, speaker?},
// frisket/preview/transcribe.py) drop the redundant positional index.
const TRANSCRIPT_SEGMENT_HIDDEN_KEYS: ReadonlySet<string> = new Set(['segment_index']);

/** An array of entity mentions: every item is an object carrying `text` and
 *  `type` strings plus at least one entity-specific offset/scoring field. The
 *  extra-key guard keeps a generic `{text, type}` json list from being treated
 *  as entities and stripped. */
function isEntityArray(items: Record<string, unknown>[]): boolean {
  return items.every(
    (it) =>
      typeof it.text === 'string' &&
      typeof it.type === 'string' &&
      ('start' in it || 'end' in it || 'fingerprint' in it || 'score' in it),
  );
}

/** An array of timestamped transcript segments — every item carries the
 *  `segment_index` the preview normalizer stamps on. */
function isTranscriptSegmentArray(items: Record<string, unknown>[]): boolean {
  return items.every((it) => 'segment_index' in it && 'text' in it);
}

function JsonValue({
  value,
  isEmailAttachments = false,
}: {
  value: CellValue;
  isEmailAttachments?: boolean;
}) {
  const { chromePreferences: { projectId } } = useWorkspaceStores();
  const raw = String(value);
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return <pre className="row-field-json">{linkify(raw)}</pre>;
  }

  if (Array.isArray(parsed) && parsed.length > 0) {
    // The email importer emits an exact homogeneous attachment list. Keep its
    // presentation intentionally small: filenames are same-origin downloads,
    // never previews or new browsing contexts. Anything malformed or mixed
    // remains on the generic JSON renderer below.
    const downloads = isEmailAttachments ? parsed.map(downloadBlobEnvelope) : [];
    if (isEmailAttachments && downloads.every((download) => download !== null)) {
      return (
        <ul className="json-blob-downloads" data-testid="json-blob-downloads">
          {downloads.map((download) => {
            const attachment = download as DownloadBlobEnvelope;
            return (
              <li key={attachment.blob}>
                <a
                  className="row-field-link"
                  href={projectBlobUrl(projectId, attachment.blob)}
                  download={attachment.filename}
                >
                  {attachment.filename}
                </a>
              </li>
            );
          })}
        </ul>
      );
    }
    // Array of objects (e.g. web_search results) → mini table with links.
    if (parsed.every((x) => x !== null && typeof x === 'object' && !Array.isArray(x))) {
      const items = parsed as Record<string, unknown>[];
      // Entity mentions ({text, type, start, end, score, fingerprint?}) show
      // only the two fields that read as data — text and type. The offsets,
      // model score, and grouping fingerprint are plumbing, not content.
      if (isEntityArray(items)) {
        return (
          <JsonMiniTable
            items={items}
            hiddenKeys={ENTITY_HIDDEN_KEYS}
            testId="entity-mini-table"
            disableBlobThumbnails={isEmailAttachments}
          />
        );
      }
      // Timestamped transcript segments carry a redundant positional index
      // (`segment_index`); the start/end timecodes already order them.
      if (isTranscriptSegmentArray(items)) {
        return (
          <JsonMiniTable
            items={items}
            hiddenKeys={TRANSCRIPT_SEGMENT_HIDDEN_KEYS}
            testId="transcript-segment-mini-table"
            disableBlobThumbnails={isEmailAttachments}
          />
        );
      }
      return <JsonMiniTable items={items} maxKeys={6} disableBlobThumbnails={isEmailAttachments} />;
    }
    // Array of scalars → list.
    if (parsed.every((x) => typeof x !== 'object' || x === null)) {
      return (
        <ul className="json-list" data-testid="json-list">
          {keyedJsonItems(parsed as unknown[]).map(({ item: x, key }) => (
            <li key={key}>{linkify(cellText(x))}</li>
          ))}
        </ul>
      );
    }
  }

  return <pre className="row-field-json">{linkify(JSON.stringify(parsed, null, 2))}</pre>;
}

// ---------------------------------------------------------------------------
// "Explain this cell": the EXACT prompt the model saw and
// the raw text it returned, from the run's best-effort trace sidecar
// exposed through the action trace endpoint. Everything renders as TEXT nodes
// — raw model output is never treated as HTML, so a malicious response can't
// inject markup.

/** Flatten a recorded prompt (usually [{role, content}…]) to readable text. */
function formatPrompt(p: unknown): string | null {
  if (p == null) return null;
  if (typeof p === 'string') return p;
  if (Array.isArray(p)) {
    return p
      .map((m) => {
        if (m !== null && typeof m === 'object' && 'role' in m) {
          const msg = m as { role?: unknown; content?: unknown };
          const content =
            typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content, null, 2);
          return `[${String(msg.role ?? 'message')}]\n${content}`;
        }
        return typeof m === 'string' ? m : JSON.stringify(m, null, 2);
      })
      .join('\n\n');
  }
  return JSON.stringify(p, null, 2);
}

function CopyButton({ text, testId }: { text: string; testId: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="mini-btn"
      data-testid={testId}
      title="Copy to clipboard"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}
    >
      {copied ? <Check size={11} /> : <Copy size={11} />} {copied ? 'copied' : 'copy'}
    </button>
  );
}

type ExplainState =
  | { phase: 'idle' | 'loading' | 'error' }
  | { phase: 'done'; evidence: RunTraceRowEvidence };

function ExplainBlock({
  runId,
  rowId,
  columnId,
  compact = false,
  columnName,
  testId,
}: {
  runId: string;
  rowId: string;
  columnId?: string | null;
  compact?: boolean;
  columnName?: string;
  testId?: string;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<ExplainState>({ phase: 'idle' });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const label = `Explain ${columnName ?? 'this'} cell`;
  const description = 'the exact prompt and raw model output that produced this value';

  // Fetch lazily, on first expand only (event handler, not an effect).
  const toggle = () => {
    setOpen((o) => !o);
    if (state.phase !== 'idle') return;
    setState({ phase: 'loading' });
    api
      .getRunTraceRow(runId, rowId, columnId)
      .then((evidence) => setState({ phase: 'done', evidence }))
      .catch(() => setState({ phase: 'error' }));
  };
  const close = useCallback(() => setOpen(false), []);

  // Viewport-clamped + flip-above placement anchored to the trigger — the
  // same primitive EnginePicker/ModelPicker/MultiColumnPicker use, rather
  // than the panel's old inline `margin-top` flow (it rendered as a flex
  // child of the 20px action slot, so a wide trace panel had nowhere to grow
  // but off the edge of the screen).
  const panelPos = useAnchoredPosition(triggerRef, {
    enabled: open,
    align: compact ? 'right' : 'left',
    width: (_rect, viewportWidth) => Math.min(380, viewportWidth - 16),
    gap: 6,
    minHeight: 160,
  });

  // Native top-layer popover (see useNativePopover's doc comment): this is
  // what lets InspectDetailColumn's own useEscapeDismiss defer to Escape
  // here instead of closing the whole Detail column out from under it.
  // `outside: false` is deliberate — the owner wants EXPLICIT dismissal
  // only (the panel's own × button, Escape, or closing the Detail column
  // wholesale by unmounting this component), not a stray click elsewhere,
  // and definitely not the old "any keypress closes it" behavior (there was
  // no scoped Escape handling before, so a bug elsewhere on the page could
  // yank focus and read as "dismissed on typing" — this hook only ever
  // wires up Escape).
  useNativePopover(panelRef, close, { enabled: open, outside: false });

  return (
    <div className={compact ? 'explain explain-compact' : 'explain'}>
      <button
        type="button"
        ref={triggerRef}
        className={compact ? 'row-field-icon explain-toggle' : 'mini-btn explain-toggle'}
        data-testid={testId ?? (compact ? 'cell-action-explain' : 'explain-cell')}
        aria-label={compact ? label : undefined}
        title={compact ? `${label} — view ${description}` : `View ${description}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={toggle}
      >
        <FileSearch2 size={12} /> {compact ? null : 'Explain this cell'}
      </button>
      {open && (
        <div
          ref={panelRef}
          role="dialog"
          aria-label={label}
          className="explain-panel"
          data-testid="explain-panel"
          style={
            panelPos
              ? {
                  top: panelPos.top,
                  bottom: panelPos.bottom,
                  left: panelPos.left,
                  width: panelPos.width,
                  maxHeight: panelPos.maxHeight,
                }
              : { visibility: 'hidden' }
          }
        >
          <div className="explain-panel-head">
            <span className="explain-panel-title">Explain this cell</span>
            <button
              type="button"
              className="row-field-icon explain-close"
              data-testid="explain-panel-close"
              aria-label="Close explain panel"
              title="Close"
              onClick={close}
            >
              <X size={12} />
            </button>
          </div>
          {state.phase === 'loading' && <div className="explain-note">loading trace…</div>}
          {state.phase === 'error' && (
            <div className="explain-note">Could not load the trace for this run.</div>
          )}
          {state.phase === 'done' && <ExplainTraceRow evidence={state.evidence} />}
        </div>
      )}
    </div>
  );
}

function formatRunDuration(startedAt: string | null, finishedAt: string | null): string | null {
  if (!startedAt || !finishedAt) return null;
  const ms = Date.parse(finishedAt) - Date.parse(startedAt);
  if (!Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  return s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

function ExplainTraceRow({ evidence }: { evidence: RunTraceRowEvidence }) {
  const trace = evidence.trace;
  const rec = trace?.row ?? null;
  if (!rec) {
    // No per-row model trace. For a deterministic (non-LLM) op there is no
    // prompt/output to show, but the run-level facts ARE available — surface
    // them instead of dead-ending on an unavailable trace.
    const run = evidence.run ?? null;
    const duration = run ? formatRunDuration(run.startedAt, run.finishedAt) : null;
    return (
      <div
        data-testid={evidence.status === 'not_recorded' ? 'explain-empty' : 'explain-missing-row'}
      >
        {run && (
          <div className="provenance-grid explain-meta" data-testid="explain-run-facts">
            {run.actionName && (
              <>
                <span className="prov-key">action</span>
                <span>{run.actionName}</span>
              </>
            )}
            {run.model && (
              <>
                <span className="prov-key">engine</span>
                <span>{run.model}</span>
              </>
            )}
            {run.status && (
              <>
                <span className="prov-key">status</span>
                <span>{run.status}</span>
              </>
            )}
            {duration && (
              <>
                <span className="prov-key">duration</span>
                <span>{duration}</span>
              </>
            )}
            <span className="prov-key">cost</span>
            <span>{formatUsdOrNone(run.costActual)}</span>
          </div>
        )}
        <div className="explain-note">{runTraceAbsenceMessage(evidence.status)}</div>
      </div>
    );
  }
  const prompt = formatPrompt(rec.prompt);
  return (
    <>
      <div className="provenance-grid explain-meta" data-testid="explain-meta">
        {trace?.actionName && (
          <>
            <span className="prov-key">action</span>
            <span>{trace.actionName}</span>
          </>
        )}
        {trace?.model && (
          <>
            <span className="prov-key">model</span>
            <span>{trace.model}</span>
          </>
        )}
        <span className="prov-key">latency</span>
        <span>{rec.latencyMs != null ? `${rec.latencyMs.toLocaleString()} ms` : '—'}{rec.cached ? ' · cached' : ''}</span>
        <span className="prov-key">tokens</span>
        <span>
          {rec.tokensIn != null || rec.tokensOut != null
            ? `${rec.tokensIn ?? '—'} in / ${rec.tokensOut ?? '—'} out`
            : '—'}
        </span>
        {rec.retries > 0 && (
          <>
            <span className="prov-key">retries</span>
            <span>{rec.retries}</span>
          </>
        )}
        {rec.error && (
          <>
            <span className="prov-key">error</span>
            <span className="explain-error">{rec.error}</span>
          </>
        )}
      </div>

      <div className="explain-section-head">
        <span>Prompt</span>
        {prompt && <CopyButton text={prompt} testId="explain-copy-prompt" />}
      </div>
      {prompt ? (
        <pre className="explain-pre" data-testid="explain-prompt">{prompt}</pre>
      ) : (
        <div className="explain-note">No prompt captured — this op ran without a model call.</div>
      )}

      <div className="explain-section-head">
        <span>Raw model output</span>
        {rec.rawResponse && <CopyButton text={rec.rawResponse} testId="explain-copy-raw" />}
      </div>
      {rec.rawResponse ? (
        <pre className="explain-pre" data-testid="explain-raw">{rec.rawResponse}</pre>
      ) : (
        <div className="explain-note">No raw output captured for this row.</div>
      )}
    </>
  );
}

function runTraceAbsenceMessage(status: RunTraceRowEvidence['status']): ReactNode {
  switch (status) {
    case 'not_recorded':
      return 'Trace logging was unavailable or the trace was dropped for this run.';
    case 'row_not_in_run':
      return 'This row is not in the run trace. It may not have been part of the run.';
    case 'missing':
      return 'Trace evidence for this row is missing.';
    case 'recorded':
      return 'Trace evidence for this row was recorded, but no row payload is available.';
  }
}

function ProvenanceBlock({ prov }: { prov: CellProvenance }) {
  const confidence =
    typeof prov.confidence === 'number' && Number.isFinite(prov.confidence)
      ? prov.confidence
      : null;
  const isRunResult = prov.currentValueRef.kind === 'run_result';
  return (
    <div className="provenance" data-testid="cell-provenance">
      <div className="provenance-grid">
        <span className="prov-key">origin</span>
        <span data-testid="cell-provenance-origin">
          {cellOriginLabel(prov.currentValueRef.kind)}
        </span>
        {isRunResult && (
          <>
            <span className="prov-key">action</span>
            <span>{prov.actionName}</span>
            <span className="prov-key">model</span>
            <span>{prov.model}</span>
            <span className="prov-key">cost</span>
            <span>{formatUsd(prov.cost)}</span>
            <span className="prov-key">confidence</span>
            <span>
              {confidence === null ? '—' : (
                <span className={`conf-pill ${confClass(confidence)}`}>{confidence.toFixed(2)}</span>
              )}
            </span>
          </>
        )}
        {prov.currentValueRef.kind === 'manual_edit' && prov.currentValueRef.opId && (
          <>
            <span className="prov-key">op</span>
            <span>{prov.currentValueRef.opId}</span>
          </>
        )}
        {prov.currentValueRef.kind === 'source_cell' && (
          <>
            <span className="prov-key">column</span>
            <span>{prov.currentValueRef.columnId}</span>
          </>
        )}
        {prov.currentValueRef.kind === 'missing' && (
          <>
            <span className="prov-key">state</span>
            <span>missing</span>
          </>
        )}
      </div>
      {prov.justification && (
        <p className="prov-justification" data-testid="prov-justification">{prov.justification}</p>
      )}
    </div>
  );
}

function cellOriginLabel(kind: CellProvenance['currentValueRef']['kind']): string {
  switch (kind) {
    case 'manual_edit':
      return 'manual edit';
    case 'run_result':
      return 'run result';
    case 'source_cell':
      return 'source cell';
    case 'compacted':
      return 'compacted evidence';
    case 'missing':
      return 'missing';
    default:
      return 'unknown';
  }
}
