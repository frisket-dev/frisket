import {
  httpContract,
  type ContractErrorFactory,
  type HttpContractSuccessResponse,
  type RequestOptions,
} from '../httpContract';
import type {
  BoundedCellValue,
  CellValue,
  CellValueRef,
  ColumnStats,
  ReplayPendingValue,
  SheetDataPage,
  DeleteSheetResult,
  SheetMeta,
  SheetRowLocation,
} from '../types';
import { cellValueFromWire } from '../wireValues';

type ColumnStatsWire = HttpContractSuccessResponse<'tenant.column_stats.get'>;
type SheetWire = HttpContractSuccessResponse<'tenant.update_sheet.patch'>;
type SheetDeleteWire = HttpContractSuccessResponse<'tenant.delete_sheet.delete'>;
type SheetDataWire = HttpContractSuccessResponse<'tenant.sheet_data.get'>;
type SheetListWire = HttpContractSuccessResponse<'tenant.list_sheets.get'>;
type SheetColumnWire = SheetDataWire['columns'][number];
type SheetRowLocationWire = HttpContractSuccessResponse<'tenant.locate_sheet_row.get'>;

export interface SheetDataContractQuery {
  selection?: string;
  view_id?: number;
  offset?: number;
  limit?: number;
  column_ids?: string;
  value_mode?: 'exact' | 'bounded';
  parent_row_id?: number;
  filter?: string;
  sort?: string;
  row_ids?: string;
  scope_row_ids?: string;
}

export interface LocateSheetRowContractQuery {
  selection?: string;
  view_id?: number;
  page_size: number;
  parent_row_id?: number;
  filter?: string;
  sort?: string;
  row_ids?: string;
  scope_row_ids?: string;
}

function mapSheetList(wire: SheetListWire): SheetMeta[] {
  const sheetById = new Map(wire.map((sheet) => [sheet.id, sheet]));
  return wire.map((sheet) => {
    const meta: SheetMeta = {
      id: String(sheet.id),
      name: sheet.name,
      rowCount: sheet.rows,
      columns: sheet.columns.map(mapSheetColumn),
      titleColumnId: sheet.title_column_id == null ? null : String(sheet.title_column_id),
      citedColumnIds: sheet.cited_column_ids.map(String),
      annotatedTextColumnIds: sheet.annotated_text_column_ids.map(String),
      documentTextColumnIds: sheet.document_text_column_ids.map(String),
      dependentSheetIds: (sheet.dependent_sheet_ids ?? []).map(String),
      refreshable: sheet.refreshable,
    };
    if (sheet.syncState === 'synced' || sheet.syncState === 'stale'
      || sheet.syncState === 'unknown' || sheet.syncState === 'checking') {
      meta.syncState = sheet.syncState;
      meta.staleReason = sheet.stale_reason ?? null;
    }
    if (sheet.materialized_kind === 'edge' || sheet.materialized_kind === 'join') {
      meta.materializedKind = sheet.materialized_kind;
    }
    const parent = sheet.parent_sheet_id == null
      ? undefined
      : sheetById.get(sheet.parent_sheet_id);
    if (parent) {
      meta.parent = {
        sheetId: String(parent.id),
        sheetName: parent.name,
        viaAction: sheet.op_label || sheet.op_kind || 'derive',
      };
    }
    return meta;
  });
}

function mapSheetColumn(column: SheetColumnWire): SheetMeta['columns'][number] {
  return {
    id: String(column.id),
    name: column.name,
    type: column.type,
    width: column.type === 'text' ? 240 : undefined,
    format: column.format,
    semanticType: column.semantic_type ?? null,
    defaultHidden: column.default_hidden,
    currentRunId: column.current_run_id == null ? null : String(column.current_run_id),
    latestRunId: column.latest_run_id == null ? null : String(column.latest_run_id),
    generationManaged: column.generation_managed,
    mixedOrigins: column.mixed_origins,
    transcriptStatus: column.transcript_status,
    mediaDownloadCandidate: column.media_download_candidate,
    replayPendingCount: column.replay_comparison_available === false ? null : column.replay_pending_count ?? 0,
    ...(column.replay_comparison_available === false ? { replayComparisonAvailable: false } : {}),
    ...(column.ai_generated ? {
      ai: {
        actionName: 'AI run',
        prompt: '',
        model: '',
        costSoFar: 0,
        versions: [],
      },
    } : {}),
  };
}

function mapUpdatedSheet(wire: SheetWire): void {
  void wire;
  return undefined;
}

function isCellValueRefKind(value: string): value is CellValueRef['kind'] {
  return value === 'manual_edit'
    || value === 'run_result'
    || value === 'source_cell'
    || value === 'missing'
    || value === 'compacted';
}

function mapCellValueRef(
  wire: SheetDataWire['rows'][number]['meta'][string]['current_value_ref'],
): CellValueRef | null {
  if (!isCellValueRefKind(wire.kind)) return null;
  return {
    kind: wire.kind,
    rowId: String(wire.row_id),
    columnId: String(wire.column_id),
    runId: wire.run_id == null ? null : String(wire.run_id),
    opId: wire.op_id == null ? null : String(wire.op_id),
  };
}

type SheetRowsContractPage = Pick<SheetDataPage, 'columns' | 'rows'>;

function mapSheetRows(wire: SheetDataWire): SheetRowsContractPage {
  const columns = wire.columns.map(mapSheetColumn);
  const columnById = new Map(wire.columns.map((column) => [String(column.id), column]));
  return {
    columns,
    rows: wire.rows.map((row, index) => {
      const cells: Record<string, CellValue> = {};
      for (const [columnId, value] of Object.entries(row.cells)) {
        cells[columnId] = cellValueFromWire(value);
      }
      const provenance: SheetDataPage['rows'][number]['provenance'] = {};
      const cellStates: Record<string, string> = {};
      const cellErrors: Record<string, string> = {};
      const cellOutcomes: Record<string, string> = {};
      const invalidCells: Record<string, true> = {};
      const replayPending: Record<string, ReplayPendingValue> = {};
      const boundedValues: Record<string, BoundedCellValue> = {};
      const sourceLinks: Record<string, { label: string }> = {};
      const replayComparisonUnavailable: Record<string, true> = {};
      for (const [columnId, meta] of Object.entries(row.meta)) {
        if (meta.source_link) sourceLinks[columnId] = meta.source_link;
        if (meta.replay_comparison_unavailable) replayComparisonUnavailable[columnId] = true;
        if (meta.bounded_value) boundedValues[columnId] = meta.bounded_value;
        if (meta.state) cellStates[columnId] = meta.state;
        if (meta.error) cellErrors[columnId] = meta.error;
        if (meta.outcome) cellOutcomes[columnId] = meta.outcome;
        if (meta.invalid) invalidCells[columnId] = true;
        if (meta.pending_value) {
          replayPending[columnId] = {
            freshValue: cellValueFromWire(meta.pending_value.fresh_value),
            runId: String(meta.pending_value.run_id),
            generatedValueHash: meta.pending_value.generated_value_hash,
          };
        }
        const ref = mapCellValueRef(meta.current_value_ref);
        const confidence = typeof meta.confidence === 'number' && Number.isFinite(meta.confidence)
          ? meta.confidence
          : null;
        const column = columnById.get(columnId);
        if (ref || confidence !== null || meta.justification || meta.error || meta.warnings?.length) {
          provenance[columnId] = {
            currentValueRef: ref ?? {
              kind: 'missing',
              rowId: String(row.id),
              columnId,
              runId: null,
              opId: null,
            },
            model: '',
            actionName: column?.ai_generated ? 'AI run' : '',
            cost: 0,
            confidence,
            justification: meta.justification ?? (meta.error ? `Error: ${meta.error}` : ''),
            ...(meta.warnings?.length ? { warnings: meta.warnings } : {}),
            runId: ref?.kind === 'run_result' && ref.runId ? ref.runId : '',
          };
        }
      }
      return {
        id: String(row.id),
        index,
        cells,
        ...(Object.keys(sourceLinks).length > 0 ? { sourceLinks } : {}),
        ...(Object.keys(boundedValues).length > 0 ? { boundedValues } : {}),
        ...(Object.keys(replayComparisonUnavailable).length > 0 ? { replayComparisonUnavailable } : {}),
        provenance,
        cellStates,
        cellErrors,
        cellOutcomes,
        ...(Object.keys(invalidCells).length > 0 ? { invalidCells } : {}),
        parentRowId: row.parent_row_id == null ? null : String(row.parent_row_id),
        childCount: row.child_count,
        ...(Object.keys(replayPending).length > 0 ? { replayPending } : {}),
      };
    }),
  };
}

function mapSheetData(wire: SheetDataWire): SheetDataPage {
  if (wire.total == null) {
    throw new Error('Full sheet-data response omitted its exact total');
  }
  return { ...mapSheetRows(wire), total: wire.total };
}

function mapColumnStats(wire: ColumnStatsWire): ColumnStats {
  return {
    schemaVersion: wire.schema_version,
    sheetId: String(wire.sheet_id),
    column: {
      id: String(wire.column.id),
      name: wire.column.name,
      type: wire.column.type,
      format: wire.column.format as ColumnStats['column']['format'],
    },
    rowCount: wire.row_count,
    threshold: wire.threshold,
    computed: wire.computed,
    requiresManualAnalyze: wire.requires_manual_analyze,
    ...(wire.missing === undefined ? {} : { missing: wire.missing }),
    ...(wire.invalid === undefined ? {} : { invalid: wire.invalid }),
    ...(wire.present === undefined ? {} : { present: wire.present }),
    ...(wire.distinct === undefined ? {} : { distinct: wire.distinct }),
    topValues: wire.top_values ?? [],
    numeric: wire.numeric ?? null,
    text: wire.text
      ? {
        count: wire.text.count,
        shortest: wire.text.shortest,
        shortestLength: wire.text.shortest_length,
        longest: wire.text.longest,
        longestLength: wire.text.longest_length,
        meanLength: wire.text.mean_length,
        medianLength: wire.text.median_length,
        lengthHistogram: wire.text.length_histogram,
      }
      : null,
    date: wire.date ?? null,
    file: wire.file
      ? {
        count: wire.file.count,
        minSize: wire.file.min_size,
        maxSize: wire.file.max_size,
      }
      : null,
    jsonTypes: wire.json_types ?? [],
  };
}

function mapSheetRowLocation(wire: SheetRowLocationWire): SheetRowLocation {
  return {
    found: wire.found,
    rowId: String(wire.row_id),
    index: wire.index,
    pageOffset: wire.page_offset,
    pageSize: wire.page_size,
  };
}

export function listSheetsContract(
  projectId: string,
  options: RequestOptions & { errorFactory?: ContractErrorFactory } = {},
): Promise<SheetMeta[]> {
  return httpContract('tenant.list_sheets.get', {
    pathParams: { pid: projectId },
    signal: options.signal,
    errorFactory: options.errorFactory,
  }, mapSheetList);
}

export function updateSheetContract(
  projectId: string,
  sheetId: number,
  titleColumnId: number | null,
  options: RequestOptions = {},
): Promise<void> {
  return httpContract('tenant.update_sheet.patch', {
    pathParams: { pid: projectId, sheet_id: sheetId },
    body: { title_column_id: titleColumnId },
    signal: options.signal,
  }, mapUpdatedSheet);
}

export function deleteSheetContract(
  projectId: string,
  sheetId: number,
  options: RequestOptions = {},
): Promise<DeleteSheetResult> {
  return httpContract('tenant.delete_sheet.delete', {
    pathParams: { pid: projectId, sheet_id: sheetId },
    signal: options.signal,
  }, (wire: SheetDeleteWire) => ({
    deletedSheetId: String(wire.deleted_sheet_id),
    deletedSheetName: wire.deleted_sheet_name,
  }));
}

export function getSheetDataContract(
  projectId: string,
  sheetId: number,
  query: SheetDataContractQuery,
  options: RequestOptions = {},
): Promise<SheetDataPage> {
  return httpContract('tenant.sheet_data.get', {
    pathParams: { pid: projectId, sheet_id: sheetId },
    query,
    signal: options.signal,
  }, mapSheetData);
}

export function getSheetRowsContract(
  projectId: string,
  sheetId: number,
  query: SheetDataContractQuery,
  options: RequestOptions = {},
): Promise<SheetRowsContractPage> {
  return httpContract('tenant.sheet_data.get', {
    pathParams: { pid: projectId, sheet_id: sheetId },
    query: { ...query, include_scope_metadata: false },
    signal: options.signal,
  }, mapSheetRows);
}

export function getColumnStatsContract(
  projectId: string,
  sheetId: number,
  columnId: number,
  query: Pick<SheetDataContractQuery, 'filter' | 'sort' | 'scope_row_ids' | 'parent_row_id' | 'selection' | 'view_id'> & { force?: boolean },
  options: RequestOptions = {},
): Promise<ColumnStats> {
  return httpContract('tenant.column_stats.get', {
    pathParams: { pid: projectId, sheet_id: sheetId, column_id: columnId },
    query,
    signal: options.signal,
  }, mapColumnStats);
}

export function locateSheetRowContract(
  projectId: string,
  sheetId: number,
  rowId: number,
  query: LocateSheetRowContractQuery,
  options: RequestOptions = {},
): Promise<SheetRowLocation> {
  return httpContract('tenant.locate_sheet_row.get', {
    pathParams: { pid: projectId, sheet_id: sheetId, row_id: rowId },
    query,
    signal: options.signal,
  }, mapSheetRowLocation);
}
