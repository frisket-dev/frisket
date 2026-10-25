import './temporal/temporal-actions.css';
import './ActivityProgress.css';
import './RowDrawer.css';
import { Button } from './Button';
import { TextArea } from './FormControls';
import { AlertTriangle, Check, Copy, Pencil, RefreshCcw, X, Zap } from 'lucide-react';
import { useEffect, useReducer, useRef, useState, type FocusEvent, type ReactNode, type RefObject } from 'react';
import type { CellValue, ColumnDef, Row, SheetMeta } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useSelector } from '../bind/useSelector';
import { presentationFor } from '../grid/typeRegistry';
import { previewColumnDef, previewValueRow } from '../grid/previewCells';
import type { PreviewCellDetail } from '../api/types';
import { isTemporalColumnType, summarizeTemporalCell } from '../temporal/model';
import { replaceTimelineItemsFromDraft, timelineItemsDraftText, type EditableTimelineType } from '../temporal/draft';
import { RowInspectorEvidenceSectionFrame } from '../workbench/contributions';
import { documentMediaColumns } from '../workbench/documentMedia';
import type { WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import type { RowTextTarget } from '../state/detailStore';
import { ClampedField, FieldValue } from './rowInspection/FieldValue';
import { CellEvidenceBlock } from './rowInspection/CellEvidence';
import { RowSourceDocuments } from './rowInspection/RowSourceDocuments';
import { ExplainBlock, ProvenanceBlock } from './rowInspection/CellRunDetails';

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
  /** Scopes ephemeral disclosure state when sheet and column IDs repeat across projects. */
  projectId: string;
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
  onOpenRowRef?(sheetId: string, rowId: string): void;
  onOpenSourceDocument?(sheetId: string, rowId: string, columnId: string): void;
  /** Deliberate per-row retry of a terminal empty_output cell (the
   *  empty-output terminal-row contract): run.backfill with this row's exact
   *  id. The promise settles when the retry run finishes. */
  onRetryCell?(columnName: string): Promise<void>;
  /** Session-local run-result disclosures, owned above a transient row fetch. */
  expandedRunResultFields: ReadonlySet<string>;
  onToggleRunResultField(fieldKey: string): void;
}

/** The Inspect content — lineage chips, per-field blocks, plugin detail
 *  contributions — WITHOUT any panel chrome. Hosted by the resident Detail
 *  column; the overlay Drawer wrapper is retired. */
export function RowDrawerBody({
  projectId,
  sheet,
  row,
  selectedColumnId,
  onEdit,
  detailContributions = EMPTY_CONTRIBUTIONS,
  renderDetailContribution,
  bodyRef,
  onOpenInDocumentView,
  onOpenRowRef,
  onOpenSourceDocument,
  onRetryCell,
  expandedRunResultFields,
  onToggleRunResultField,
}: RowDrawerBodyProps) {
  const { detail } = useWorkspaceStores();
  const rowTextTarget = useSelector(detail.store, (state) => state.rowTextTarget);
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
      {onOpenSourceDocument && (
        <RowSourceDocuments key={row.id} rowId={String(row.id)} onOpenDocument={onOpenSourceDocument} />
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
      {row.parentRowId && sheet.parent && onOpenRowRef && (
        <button
          type="button"
          className="row-lineage"
          data-testid="row-lineage"
          onClick={() => onOpenRowRef(sheet.parent!.sheetId, row.parentRowId!)}
          title={`Open the source row in ${sheet.parent.sheetName}`}
        >
          ↰ derived from <strong>{sheet.parent.sheetName}</strong> via {sheet.parent.viaAction}
        </button>
      )}
      {sheet.columns.map((col) => {
        const fieldKey = `${projectId}:${sheet.id}:${col.id}`;
        return (
          <RowField
            key={`${row.id}:${col.id}`}
            col={col}
            columns={sheet.columns}
            row={row}
            sheetId={sheet.id}
            selected={col.id === selectedColumnId}
            textTarget={selectedColumnId === col.id && rowTextTarget?.sheetId === sheet.id && rowTextTarget.rowId === row.id
              && rowTextTarget.columnId === col.id ? rowTextTarget : undefined}
            onEdit={onEdit}
            onRetryCell={onRetryCell}
            runResultExpanded={expandedRunResultFields.has(fieldKey)}
            onToggleRunResult={() => onToggleRunResultField(fieldKey)}
          />
        );
      })}
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
  textTarget,
  onEdit,
  onRetryCell,
  runResultExpanded,
  onToggleRunResult,
}: {
  col: ColumnDef;
  columns: ColumnDef[];
  row: Row;
  sheetId?: string;
  selected: boolean;
  textTarget?: RowTextTarget;
  onEdit(row: Row, col: ColumnDef, value: CellValue): Promise<void>;
  onRetryCell?(columnName: string): Promise<void>;
  /** Controlled by RowDrawerBody so this field stays expanded across rows. */
  runResultExpanded: boolean;
  onToggleRunResult(): void;
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
  const bounded = row.boundedValues?.[col.id];
  const loaded = !row.loadedColumnIds || row.loadedColumnIds.includes(col.id);
  const canEdit = loaded && !bounded && (editableTimelineType
    ? temporalDraftText !== null
    : !col.ai && !isTemporalColumnType(col.type));
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
            {col.ai && runId ? (
              <ExplainBlock
                key={`${runId}:${row.id}:${prov?.currentValueRef?.columnId ?? col.id}`}
                runId={runId}
                rowId={row.id}
                columnId={prov?.currentValueRef?.columnId ?? col.id}
                compact
                columnName={col.name}
                testId="cell-action-explain"
                visible={actionsVisible}
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
            {actionsVisible && loaded && !bounded ? (
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
      {editing && canEdit ? (
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
          <TextArea
            className="row-field-input"
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
          <Button
            type="submit"

            data-testid={`cell-save-${col.name}`}
            disabled={Boolean(temporalDraftResult && !temporalDraftResult.ok)}
          >
            <Check size={12} /> Save
          </Button>
        </form>
      ) : bounded || textTarget ? (
        <FieldValue col={col} columns={columns} row={row} sheetId={sheetId} value={displayValue} textTarget={textTarget} />
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
      {row.replayComparisonUnavailable?.[col.id] && (
        <p className="muted" data-testid="replay-comparison-unavailable">Updated-value comparison is unavailable above 64 KiB.</p>
      )}
      {prov?.warnings?.length ? (
        <aside className="form-hint" role="note" aria-label={`${col.name} run warnings`} data-testid="cell-warnings">
          <strong><AlertTriangle size={14} className="run-warning-icon" aria-hidden /> Run warnings</strong>
          <ul>{prov.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
        </aside>
      ) : null}
      {col.ai && prov && (
        <ProvenanceBlock
          prov={prov}
          runResultExpanded={runResultExpanded}
          onToggleRunResult={onToggleRunResult}
        />
      )}
      {retryable && onRetryCell && (
        <div className="row-field-retry" data-testid={`cell-retry-${col.name}`}>
          <span className="row-field-retry-note">
            The engine returned empty output for this row — retrying may well
            do the same.
          </span>
          <Button
            type="button"

            data-testid={`cell-retry-button-${col.name}`}
            disabled={retryBusy}
            onClick={() => {
              setRetryBusy(true);
              void onRetryCell(col.name).finally(() => setRetryBusy(false));
            }}
          >
            <RefreshCcw size={11} /> {retryBusy ? 'Retrying…' : 'Retry anyway'}
          </Button>
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
