import { useCallback, useMemo, useState } from 'react';
import {
  GridCellKind,
  type GridCell,
  type GridColumn,
  type Item,
  type Theme,
} from '@glideapps/glide-data-grid';

import type { ActionComparisonBoundedValue } from '../api/actionComparison';
import type { EvaluationResponse } from '../api/evaluation';
import type { BoundedCellValue, CellValue, ColumnDef, Row } from '../api/types';
import { buildCell, typeHeaderIcon, withWrapSafeDisplayData } from '../grid/cells';
import { GridViewport, type GridViewportProps } from '../grid/GridViewport';
import { useGridTheme } from '../grid/useGridTheme';
import {
  actionCompareCellEquality,
  type ActionCompareResultCell,
  type ActionCompareSessionSnapshot,
  type ActionCompareVariant,
} from './actionCompareSession';
import {
  VARIANT_BADGE_PRESENTATION,
  variantBadgeHeaderIcons,
  variantBadgeIconName,
} from './variantBadgePresentation';
import { answerText, scoreLabel } from './evaluation/comparisonEvaluation';
import { evaluationTheme } from './evaluation/evaluationTheme';



const SOURCE_GROUP = 'action-compare:sources';
const OUTPUT_GROUP_PREFIX = 'action-compare:output:';
const DEFAULT_SOURCE_WIDTH = 140;
const DEFAULT_OUTPUT_WIDTH = 170;

interface SourceGridColumn {
  kind: 'source';
  id: string;
  column: ColumnDef;
  sourceName: string;
}

interface OutputGridColumn {
  kind: 'output';
  id: string;
  column: ColumnDef;
  logicalKey: string;
  variant: ActionCompareVariant;
}

interface ExpectedGridColumn { kind: 'expected'; id: string; column: ColumnDef; logicalKey: string }

type CompareGridColumn = SourceGridColumn | OutputGridColumn | ExpectedGridColumn;

export interface ActionCompareStructuredValue {
  column: ColumnDef;
  row: Row;
}

export interface ActionCompareGridProps {
  projectId: string;
  state: ActionCompareSessionSnapshot;
  rowHeight: number;
  wrapText: boolean;
  frozenColumnCount: number;
  labelForDraft?(draft: ActionCompareVariant['draft'], displayKey: string): string;
  onOpenBoundedValue?(args: {
    value: ActionComparisonBoundedValue;
    previewId: string | null;
    rowId: number;
    columnKey: string;
  }): void;
  onOpenStructuredValue?(value: ActionCompareStructuredValue): void;
  evaluation?: {
    response: EvaluationResponse | null; field: string; lens: 'each_other' | 'expected'; rowIds: number[];
    onOpenRow(rowId: number): void;
    onScoreClick(key: string): void;
    onRuleClick(): void;
  };
}

function displayedCell(
  variant: ActionCompareVariant,
  cell: ActionCompareResultCell | undefined,
): ActionCompareResultCell {
  if (cell) return cell;
  if (variant.status === 'pending' || variant.status === 'running') return { state: 'pending' };
  if (variant.status === 'cancelled') return { state: 'cancelled' };
  if (variant.status === 'failed' || variant.status === 'unavailable') {
    return { state: 'error', error: variant.error ?? 'Preview failed' };
  }
  return { state: 'missing' };
}

function boundedForGrid(value: ActionComparisonBoundedValue): BoundedCellValue {
  // Preview artifacts use their href rather than a retrieval revision, while
  // current comparison descriptors deliberately omit edit-policy fields. The
  // shared grid renderer reads only these display facts; opening the value
  // keeps the original descriptor and callback below.
  return {
    preview: value.preview,
    value_kind: value.value_kind,
    total_characters: value.total_characters,
    total_bytes: value.total_bytes,
    retrieval_revision: 'retrieval_revision' in value ? value.retrieval_revision : 0,
    truncated: true,
    manual_editable: false,
    manual_edit_byte_limit: 0,
  };
}

function gridValue(value: ActionCompareResultCell['value']): CellValue {
  if (value === undefined || value === null) return null;
  if (typeof value === 'object') return JSON.stringify(value);
  return value;
}

function rowForCell(
  rowId: number,
  rowIndex: number,
  columnId: string,
  cell: ActionCompareResultCell,
): Row {
  const row: Row = {
    id: String(rowId),
    index: rowIndex,
    cells: { [columnId]: null },
    provenance: {},
  };
  if (cell.bounded_value) row.boundedValues = { [columnId]: boundedForGrid(cell.bounded_value) };
  else if (cell.evaluationValue) row.boundedValues = { [columnId]: { ...cell.evaluationValue,
    value_kind: 'text', retrieval_revision: 0, manual_editable: false, manual_edit_byte_limit: 0,
  } };
  if (cell.state === 'value') row.cells[columnId] = gridValue(cell.value);
  if (cell.state === 'error') row.cellErrors = { [columnId]: cell.error || 'Row failed' };
  return row;
}

function statusText(cell: ActionCompareResultCell): string | null {
  if (cell.state === 'cancelled') return 'Cancelled';
  if (cell.state === 'missing') return 'No result';
  return null;
}

type ProjectedCompareCell = GridCell & {
  actionCompareFacts?: readonly string[];
  actionCompareComparison?: ReturnType<typeof actionCompareCellEquality>;
};

function withFacts(
  gridCell: GridCell,
  facts: readonly string[],
  comparison: ReturnType<typeof actionCompareCellEquality> | undefined,
  themeOverride?: Partial<Theme>,
): GridCell {
  return {
    ...gridCell,
    readonly: true,
    actionCompareFacts: facts,
    actionCompareComparison: comparison,
    ...(themeOverride ? { themeOverride: { ...gridCell.themeOverride, ...themeOverride } } : {}),
  } as ProjectedCompareCell;
}

function compareColumns(
  state: ActionCompareSessionSnapshot,
  evaluation?: ActionCompareGridProps['evaluation'],
): CompareGridColumn[] {
  const hidden = new Set(state.hiddenColumnKeys);
  const sourceColumns: SourceGridColumn[] = state.context.source_columns
    .filter((column) => !hidden.has(`source:${column.name}`))
    .map((column) => {
      const id = `source:${column.column_id}`;
      return {
        kind: 'source',
        id,
        sourceName: column.name,
        column: {
          id,
          name: column.name,
          type: column.column_type as ColumnDef['type'],
          format: column.format,
        },
      };
    });
  const outputColumns: OutputGridColumn[] = state.context.output_columns
    .filter((column) => !hidden.has(`output:${column.logical_key}`) && (!evaluation?.field || column.logical_key === evaluation.field))
    .flatMap((column) => state.variants.map((variant) => {
      const id = `output:${column.logical_key}:${variant.id}`;
      return {
        kind: 'output' as const,
        id,
        logicalKey: column.logical_key,
        variant,
        column: {
          id,
          name: column.current_name,
          type: column.column_type as ColumnDef['type'],
          format: column.format,
          semanticType: column.semantic_type,
        },
      };
    }));
  const expectedColumns: ExpectedGridColumn[] = evaluation?.lens === 'expected' && evaluation.response?.link
    ? state.context.output_columns.filter((column) => column.logical_key === evaluation.field).map((column) => ({
      kind: 'expected', id: `expected:${column.logical_key}`, logicalKey: column.logical_key,
      column: { id: `expected:${column.logical_key}`, name: 'expected', type: 'text', format: null },
    })) : [];
  return [...sourceColumns, ...expectedColumns, ...outputColumns];
}

export function ActionCompareGrid({ projectId, state, rowHeight, wrapText, frozenColumnCount,
  labelForDraft, onOpenBoundedValue, onOpenStructuredValue, evaluation }: ActionCompareGridProps) {
  const { gridTheme, aiColumnTheme, gridCellPalette, differenceTheme } = useGridTheme();
  const evaluationColors = useMemo(() => evaluationTheme(gridTheme), [gridTheme]);
  const variantColumnTheme = useMemo(() => ({
    ...aiColumnTheme,
    headerIconSize: VARIANT_BADGE_PRESENTATION.size,
  }), [aiColumnTheme]);
  const sampleRowIds = evaluation?.rowIds ?? state.context.sample_row_ids;
  const projectedColumns = useMemo(() => compareColumns(state, evaluation), [state, evaluation]);
  const [widths, setWidths] = useState<Record<string, number>>({});
  const [activeFacts, setActiveFacts] = useState<string | null>(null);
  const outputNames = useMemo(() => new Map(
    state.context.output_columns.map((column) => [
      `${OUTPUT_GROUP_PREFIX}${column.logical_key}`,
      column.current_name,
    ]),
  ), [state.context.output_columns]);
  const columns = useMemo<GridColumn[]>(() => projectedColumns.map((projected) => {
    const output = projected.kind === 'output';
    const icon = output
      ? variantBadgeIconName(projected.variant.displayKey) ?? 'aiBolt'
      : typeHeaderIcon(projected.column.type);
    const field = projected.kind === 'source' ? undefined : evaluation?.response?.link?.fields.find((item) => item.logical_key === projected.logicalKey);
    const score = output ? evaluation?.response?.variants.find((variant) => variant.key === projected.variant.displayKey)?.scores[projected.logicalKey] : undefined;
    const checked = projected.kind === 'expected' ? evaluation?.response?.variants[0]?.scores[projected.logicalKey]?.checked : undefined;
    const partialHand = field?.rule === 'hand' && score && score.remaining > 0;
    return {
      id: projected.id,
      title: output
        ? evaluation?.lens === 'expected'
          ? `${variantBadgeIconName(projected.variant.displayKey) ? '' : `${projected.variant.displayKey} · `}${scoreLabel(score?.accuracy)}${partialHand ? ` · ${score.scored}/${score.scored + score.remaining} scored` : ''}`
          : `${variantBadgeIconName(projected.variant.displayKey) ? '' : `${projected.variant.displayKey} · `}${labelForDraft?.(
          projected.variant.draft,
          projected.variant.displayKey,
        ) ?? projected.variant.label}`
        : projected.kind === 'expected' ? `expected · ${field?.rule ?? 'exact'}${checked != null ? ` · ${checked} checked` : ''}` : projected.column.name,
      group: projected.kind !== 'source' ? `${OUTPUT_GROUP_PREFIX}${projected.logicalKey}` : SOURCE_GROUP,
      width: widths[projected.id] ?? (partialHand || projected.kind === 'expected' ? 240 : output ? DEFAULT_OUTPUT_WIDTH : DEFAULT_SOURCE_WIDTH),
      ...(icon ? { icon } : {}),
      ...(projected.kind === 'expected' ? { themeOverride: { bgHeader: evaluationColors.expected, textHeader: evaluationColors.match } }
        : output ? { themeOverride: { ...variantColumnTheme,
          ...(evaluation?.lens === 'expected' && evaluation.response?.best[projected.logicalKey]?.includes(projected.variant.displayKey) ? { textHeader: evaluationColors.match } : {}),
        } } : {}),
    };
  }), [evaluation, evaluationColors, labelForDraft, projectedColumns, variantColumnTheme, widths]);

  const cellAt = useCallback((projected: CompareGridColumn, rowId: number): ActionCompareResultCell | undefined => {
    if (projected.kind === 'source') {
      return state.context.source_rows[String(rowId)]?.[projected.sourceName];
    }
    if (projected.kind === 'expected') {
      const value = evaluation?.response?.expected[String(rowId)]?.[projected.logicalKey];
      return { state: value == null || value === '' ? 'null' : 'value', value,
        evaluationValue: evaluation?.response?.expected_bounded?.[String(rowId)]?.[projected.logicalKey] } as ActionCompareResultCell;
    }
    const evaluated = evaluation?.response?.variants.find((variant) => variant.key === projected.variant.displayKey)?.rows[String(rowId)]?.[projected.logicalKey];
    if (evaluated) {
      const { bounded_value, ...cell } = evaluated;
      return { ...cell, evaluationValue: bounded_value };
    }
    const shown = projected.variant.result ?? projected.variant.previousResult;
    return displayedCell(
      projected.variant,
      shown?.rows[String(rowId)]?.[projected.logicalKey],
    );
  }, [evaluation, state.context.source_rows]);

  const getCellContent = useCallback((item: Item): GridCell => {
    const [columnIndex, rowIndex] = item;
    const projected = projectedColumns[columnIndex];
    const rowId = sampleRowIds[rowIndex];
    if (!projected || rowId === undefined) return { kind: GridCellKind.Loading, allowOverlay: false };
    const rawCell = cellAt(projected, rowId);
    const cell: ActionCompareResultCell = projected.kind === 'output'
      ? displayedCell(projected.variant, rawCell)
      : rawCell ?? { state: 'null' as const };
    const status = projected.kind === 'output' ? statusText(cell) : null;
    const row = rowForCell(rowId, rowIndex, projected.id, status ? { state: 'null' } : cell);
    let rendered = withWrapSafeDisplayData(buildCell(status
      ? { ...projected.column, type: 'text', format: null }
      : projected.column, row, {
      projectId,
      decodeEscapes: projected.kind === 'output',
      gridCellPalette,
      pending: cell.state === 'pending',
      wrap: wrapText,
    }));
    if (status && rendered.kind === GridCellKind.Text) {
      rendered = {
        ...rendered,
        data: '',
        displayData: status,
        copyData: '',
        themeOverride: gridCellPalette.empty,
      };
    }
    if (projected.kind === 'output' && evaluation?.lens === 'expected') {
      const verdict = evaluation.response?.variants.find((variant) => variant.key === projected.variant.displayKey)?.verdicts[String(rowId)]?.[projected.logicalKey];
      if (verdict) {
        const text = cell.state === 'error' ? `! Failed · ${cell.error || 'Action failed'}`
          : verdict === 'not_run' ? 'not run'
            : `${verdict === 'match' ? '✓ ' : verdict === 'miss' ? '✗ ' : ''}${cell.evaluationValue?.preview ?? (answerText(cell.value) || 'empty answer')}`;
        rendered = { kind: GridCellKind.Text, data: answerText(cell.value), displayData: text,
          allowOverlay: false, readonly: true, allowWrapping: wrapText,
          ...(verdict === 'miss' ? { themeOverride: { ...gridCellPalette.error, bgCell: evaluationColors.miss } } : {}),
        };
      }
    }
    if (projected.kind === 'expected') rendered = { ...rendered, themeOverride: { ...rendered.themeOverride, bgCell: evaluationColors.expected } };
    let comparison: ReturnType<typeof actionCompareCellEquality> | undefined;
    if (projected.kind === 'output' && !projected.variant.baseline
      && projected.variant.status === 'ready' && projected.variant.result) {
      const baseline = state.variants[0];
      const baselineCell = baseline.result?.rows[String(rowId)]?.[projected.logicalKey];
      comparison = actionCompareCellEquality(baselineCell, cell);
    }
    const previous = projected.kind === 'output'
      && !projected.variant.result && Boolean(projected.variant.previousResult);
    if (previous) rendered = { ...rendered, style: 'faded' };
    const facts = [
      comparison === 'different' ? 'Differs from A' : '',
      comparison === 'not_comparable' ? 'Not compared' : '',
      cell.origin?.review_decision === 'reject_clear' ? 'Cleared rejection' : '',
      cell.origin?.review_decision === 'edit' || cell.origin?.kind === 'manual_edit' ? 'Edited' : '',
      previous ? 'Previous settings' : '',
    ].filter(Boolean);
    rendered = withFacts(
      rendered,
      facts,
      comparison,
      evaluation?.lens !== 'expected' && state.highlightDifferences && comparison === 'different' ? differenceTheme : undefined,
    );
    return rendered;
  }, [cellAt, differenceTheme, evaluation, evaluationColors, gridCellPalette, projectId, projectedColumns, sampleRowIds,
    state.highlightDifferences, state.variants, wrapText]);

  const onCellActivated = useCallback((item: Item) => {
    const [columnIndex, rowIndex] = item;
    const projected = projectedColumns[columnIndex];
    const rowId = sampleRowIds[rowIndex];
    if (!projected || rowId === undefined) return;
    if (evaluation && projected.kind !== 'source') { evaluation.onOpenRow(rowId); return; }
    const facts = (getCellContent(item) as ProjectedCompareCell).actionCompareFacts ?? [];
    setActiveFacts(facts.length ? facts.join(' · ') : null);
    const rawCell = cellAt(projected, rowId);
    const cell = projected.kind === 'output'
      ? displayedCell(projected.variant, rawCell)
      : rawCell;
    if (!cell) return;
    if (cell.bounded_value && onOpenBoundedValue) {
      onOpenBoundedValue({
        value: cell.bounded_value,
        previewId: projected.kind === 'output' ? projected.variant.previewId : null,
        rowId,
        columnKey: projected.kind === 'source' ? projected.sourceName : projected.logicalKey,
      });
      return;
    }
    if (cell.state !== 'value' || cell.value === null || cell.value === undefined) return;
    const structured = projected.column.type === 'json' || typeof cell.value === 'object';
    if (!structured || !onOpenStructuredValue) return;
    onOpenStructuredValue({
      column: projected.column,
      row: rowForCell(rowId, rowIndex, projected.id, cell),
    });
  }, [cellAt, evaluation, getCellContent, onOpenBoundedValue, onOpenStructuredValue, projectedColumns, sampleRowIds]);

  const drawHeader = useCallback<NonNullable<GridViewportProps['drawHeader']>>((args, drawContent) => {
    drawContent();
    const projected = projectedColumns[args.columnIndex];
    if (projected?.kind !== 'output' || evaluation?.lens !== 'expected') return;
    const score = evaluation.response?.variants.find((variant) => variant.key === projected.variant.displayKey)?.scores[projected.logicalKey];
    if (score?.accuracy == null) return;
    const best = evaluation.response?.best[projected.logicalKey]?.includes(projected.variant.displayKey);
    const { ctx, rect } = args;
    ctx.save(); ctx.fillStyle = best ? evaluationColors.match : args.theme.textMedium;
    ctx.fillRect(rect.x + 8, rect.y + rect.height - 4, (rect.width - 16) * score.accuracy, 2);
    if (best) { ctx.strokeStyle = args.theme.accentColor; ctx.lineWidth = 2; ctx.strokeRect(rect.x + 1, rect.y + 1, rect.width - 2, rect.height - 2); }
    ctx.restore();
  }, [evaluation, evaluationColors.match, projectedColumns]);

  const onColumnResize = useCallback<NonNullable<GridViewportProps['onColumnResize']>>((column, newSize) => {
    if (!column.id) return;
    setWidths((current) => ({ ...current, [String(column.id)]: Math.round(newSize) }));
  }, []);
  const getGroupDetails = useCallback<NonNullable<GridViewportProps['getGroupDetails']>>((groupName) => {
    if (groupName === SOURCE_GROUP) return { name: 'Sources' };
    const outputName = outputNames.get(groupName);
    if (outputName === undefined) return { name: groupName };
    return {
      name: outputName,
      icon: 'aiBolt',
      overrideTheme: aiColumnTheme,
    };
  }, [aiColumnTheme, outputNames]);

  return <div className="grid-host action-compare-grid" data-testid="action-compare-grid"
    data-group-header-height="28" data-header-height="34">
    <GridViewport extraHeaderIcons={variantBadgeHeaderIcons}
      columns={columns}
      rows={sampleRowIds.length}
      getCellContent={getCellContent}
      getCellsForSelection
      theme={gridTheme}
      rowSelectionMode="multi"
      rowHeight={rowHeight}
      freezeColumns={frozenColumnCount}
      onColumnResize={onColumnResize}
      onCellActivated={onCellActivated}
      drawHeader={drawHeader}
      onHeaderClicked={(index) => {
        const projected = projectedColumns[index];
        if (projected?.kind === 'expected') evaluation?.onRuleClick();
        if (projected?.kind === 'output' && evaluation?.lens === 'expected') evaluation.onScoreClick(projected.variant.displayKey);
      }}
      getGroupDetails={getGroupDetails}
    />
    {activeFacts && <output className="action-compare-cell-status" aria-live="polite">{activeFacts}</output>}
  </div>;
}
