import { useCallback, useMemo, useState } from 'react';
import {
  GridCellKind,
  type GridCell,
  type GridColumn,
  type Item,
  type Theme,
} from '@glideapps/glide-data-grid';

import type { ActionComparisonBoundedValue } from '../api/actionComparison';
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

type CompareGridColumn = SourceGridColumn | OutputGridColumn;

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
    .filter((column) => !hidden.has(`output:${column.logical_key}`))
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
  return [...sourceColumns, ...outputColumns];
}

export function ActionCompareGrid({ projectId, state, rowHeight, wrapText, frozenColumnCount,
  labelForDraft, onOpenBoundedValue, onOpenStructuredValue }: ActionCompareGridProps) {
  const { gridTheme, aiColumnTheme, gridCellPalette, differenceTheme } = useGridTheme();
  const sampleRowIds = useMemo(() => state.context.sample_row_ids.slice(
    0,
    Math.min(20, state.context.limits.max_sample_rows),
  ), [state.context.limits.max_sample_rows, state.context.sample_row_ids]);
  const projectedColumns = useMemo(() => compareColumns(state), [state]);
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
    const icon = output ? 'aiBolt' : typeHeaderIcon(projected.column.type);
    return {
      id: projected.id,
      title: output
        ? `${projected.variant.displayKey} · ${labelForDraft?.(
          projected.variant.draft,
          projected.variant.displayKey,
        ) ?? projected.variant.label}`
        : projected.column.name,
      group: output ? `${OUTPUT_GROUP_PREFIX}${projected.logicalKey}` : SOURCE_GROUP,
      width: widths[projected.id] ?? (output ? DEFAULT_OUTPUT_WIDTH : DEFAULT_SOURCE_WIDTH),
      ...(icon ? { icon } : {}),
      ...(output ? { themeOverride: aiColumnTheme } : {}),
    };
  }), [aiColumnTheme, labelForDraft, projectedColumns, widths]);

  const cellAt = useCallback((projected: CompareGridColumn, rowId: number) => {
    if (projected.kind === 'source') {
      return state.context.source_rows[String(rowId)]?.[projected.sourceName];
    }
    const shown = projected.variant.result ?? projected.variant.previousResult;
    return displayedCell(
      projected.variant,
      shown?.rows[String(rowId)]?.[projected.logicalKey],
    );
  }, [state.context.source_rows]);

  const getCellContent = useCallback((item: Item): GridCell => {
    const [columnIndex, rowIndex] = item;
    const projected = projectedColumns[columnIndex];
    const rowId = sampleRowIds[rowIndex];
    if (!projected || rowId === undefined) return { kind: GridCellKind.Loading, allowOverlay: false };
    const rawCell = cellAt(projected, rowId);
    const cell = projected.kind === 'output'
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
      state.highlightDifferences && comparison === 'different' ? differenceTheme : undefined,
    );
    return rendered;
  }, [cellAt, differenceTheme, gridCellPalette, projectId, projectedColumns, sampleRowIds,
    state.highlightDifferences, state.variants, wrapText]);

  const onCellActivated = useCallback((item: Item) => {
    const [columnIndex, rowIndex] = item;
    const projected = projectedColumns[columnIndex];
    const rowId = sampleRowIds[rowIndex];
    if (!projected || rowId === undefined) return;
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
        columnKey: projected.kind === 'output' ? projected.logicalKey : projected.sourceName,
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
  }, [cellAt, getCellContent, onOpenBoundedValue, onOpenStructuredValue, projectedColumns, sampleRowIds]);

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
    <GridViewport
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
      getGroupDetails={getGroupDetails}
    />
    {activeFacts && <output className="action-compare-cell-status" aria-live="polite">{activeFacts}</output>}
  </div>;
}
