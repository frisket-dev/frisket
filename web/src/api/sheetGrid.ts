import { ApiError } from './contractErrors';
import type { GeneratedActionParams } from '../generated/actionTypes';
import {
  getColumnStatsContract,
  getSheetDataContract,
  getSheetRowsContract,
  deleteSheetContract,
  listSheetsContract,
  locateSheetRowContract,
  updateSheetContract,
  type LocateSheetRowContractQuery,
  type SheetDataContractQuery,
} from './httpContractRoutes';
import type {
  AddColumnResult,
  CellEdit,
  CellValue,
  ColumnDef,
  ColumnPatch,
  ColumnStats,
  ColumnType,
  DeleteRowsResult,
  DeleteSheetResult,
  SheetDataOptions,
  SheetDataPage,
  SheetMeta,
  SheetRefreshResult,
  SheetRowLocation,
  JsonValue,
  Row,
} from './types';
import type { V1ActionSession } from './v1ActionSession';

type ColumnPatchActionParams = GeneratedActionParams['column.patch'];
type ColumnAddActionParams = GeneratedActionParams['column.add'];
type ColumnSetTypeActionParams = GeneratedActionParams['column.set_type'];
type CellEditActionParams = GeneratedActionParams['cell.edit'];
type RowAddActionParams = GeneratedActionParams['row.add'];
type RowDeleteActionParams = GeneratedActionParams['row.delete'];
type ReplayAcceptActionParams = GeneratedActionParams['replay.accept'];
type ReplayAcceptColumnActionParams = GeneratedActionParams['replay.accept_column'];
type ReplayDismissActionParams = GeneratedActionParams['replay.dismiss'];

interface SheetGridColumnRunInfo {
  runId: string;
  actionName: string;
  prompt: string;
  model: string;
}

interface SheetGridDependencies {
  columnRunInfo(
    projectId: string,
    sheetId: string,
    column: { id: string; name: string },
  ): SheetGridColumnRunInfo | undefined;
  /** The project API remains the action-session composition root. */
  v1ActionSession: Pick<
    V1ActionSession,
    | 'v1ActionSpec'
    | 'registeredProjectActionSpec'
    | 'withV1ActionResult'
    | 'assertCompletedV1ActionResult'
  >;
}

interface ProjectIdentity {
  /** Matches the key used by ActionRunsApi's browser cache. */
  cacheId: string;
  /** Raw project id; generated HTTP path rendering owns percent-encoding. */
  pathId: string;
}

function jsonValueFromCell(
  value: CellValue,
  errorMessage = 'Row cell values must be JSON-compatible',
): JsonValue {
  return checkedJsonValue(value, errorMessage);
}

function checkedJsonValue(value: unknown, errorMessage: string): JsonValue {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'number') {
    if (Number.isFinite(value)) return value;
    throw new ApiError(400, errorMessage);
  }
  if (Array.isArray(value)) {
    return Array.from(value, (item) => checkedJsonValue(item, errorMessage));
  }
  if (typeof value === 'object') {
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new ApiError(400, errorMessage);
    }
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, checkedJsonValue(item, errorMessage)]),
    );
  }
  throw new ApiError(400, errorMessage);
}

function jsonCells(cells: Record<string, CellValue>): Record<string, JsonValue> {
  return Object.fromEntries(
    Object.entries(cells).map(([columnId, value]) => [columnId, jsonValueFromCell(value)]),
  );
}

function sheetDataQuery(
  offset: number,
  limit: number,
  options: SheetDataOptions | null | undefined,
): SheetDataContractQuery {
  const opts = options ?? {};
  const query: SheetDataContractQuery = { offset, limit };
  if (opts.valueMode) query.value_mode = opts.valueMode;
  if (opts.columnIds != null) query.column_ids = opts.columnIds.map(Number).join(',');
  if (opts.scopeRowIds != null) query.scope_row_ids = opts.scopeRowIds.join(',');
  if (opts.selection != null) query.selection = JSON.stringify(opts.selection);
  if (opts.viewId != null) query.view_id = opts.viewId;
  if (opts.parentRowId != null) query.parent_row_id = Number(opts.parentRowId);
  if (opts.rowIds != null) {
    query.row_ids = opts.rowIds.join(',');
  } else {
    if (opts.filter && Object.keys(opts.filter).length > 0) {
      query.filter = JSON.stringify(opts.filter);
    }
    if (opts.sort && opts.sort.length > 0) query.sort = JSON.stringify(opts.sort);
  }
  return query;
}

function locateSheetRowQuery(
  pageSize: number,
  options: SheetDataOptions | null | undefined,
): LocateSheetRowContractQuery {
  const opts = options ?? {};
  const query: LocateSheetRowContractQuery = { page_size: pageSize };
  if (opts.scopeRowIds != null) query.scope_row_ids = opts.scopeRowIds.join(',');
  if (opts.selection != null) query.selection = JSON.stringify(opts.selection);
  if (opts.viewId != null) query.view_id = opts.viewId;
  if (opts.parentRowId != null) query.parent_row_id = Number(opts.parentRowId);
  // A ranked row-id scope wins over filter/sort, including an explicit empty
  // scope. The server ignores the ids for locate but uses their presence to
  // preserve this same lens precedence.
  if (opts.rowIds != null) {
    query.row_ids = opts.rowIds.join(',');
  } else {
    if (opts.filter && Object.keys(opts.filter).length > 0) {
      query.filter = JSON.stringify(opts.filter);
    }
    if (opts.sort && opts.sort.length > 0) query.sort = JSON.stringify(opts.sort);
  }
  return query;
}

export function createSheetGridApi(dependencies: SheetGridDependencies, projectId: string) {
  const captureProjectIdentity = (): ProjectIdentity => ({ cacheId: projectId, pathId: projectId });
  function enrichAiColumn<Column extends ColumnDef>(
    project: ProjectIdentity,
    sheetId: string,
    column: Column,
  ): Column {
    if (!column.ai) return column;
    const info = dependencies.columnRunInfo(project.cacheId, sheetId, column);
    if (!info || info.runId !== (column.latestRunId ?? column.currentRunId)) return column;
    return {
      ...column,
      ai: {
        ...column.ai,
        actionName: info.actionName ?? column.ai.actionName,
        prompt: info.prompt ?? column.ai.prompt,
        model: info.model ?? column.ai.model,
      },
    };
  }

  function enrichRows(
    project: ProjectIdentity,
    sheetId: string,
    offset: number,
    data: { columns: ColumnDef[]; rows: Row[] },
  ): Row[] {
    const columnById = new Map(data.columns.map((column) => [column.id, column]));
    return data.rows.map((row, index) => ({
      ...row,
      index: offset + index,
      provenance: Object.fromEntries(
        Object.entries(row.provenance).map(([columnId, provenance]) => {
          const column = columnById.get(columnId);
          const info = column
            ? dependencies.columnRunInfo(project.cacheId, sheetId, column)
            : undefined;
          const sourceRunId = provenance.currentValueRef?.kind === 'run_result'
            ? provenance.currentValueRef.runId : null;
          if (!info || info.runId !== sourceRunId) return [columnId, provenance];
          return [columnId, {
            ...provenance,
            model: info?.model ?? provenance.model,
            actionName: info?.actionName ?? provenance.actionName,
          }];
        }),
      ),
    }));
  }

  async function getSheetDataForProject(
    project: ProjectIdentity,
    sheetId: string,
    offset: number,
    limit: number,
    options: SheetDataOptions | null | undefined,
  ): Promise<SheetDataPage> {
    const data = await getSheetDataContract(
      project.pathId,
      Number(sheetId),
      sheetDataQuery(offset, limit, options),
    );
    const columns = data.columns.map((column) =>
      enrichAiColumn(project, sheetId, column),
    );
    const rows = enrichRows(project, sheetId, offset, { ...data, columns }).map((row) => (
      options?.columnIds ? { ...row, loadedColumnIds: options.columnIds } : row
    ));
    return { rows, total: data.total, columns };
  }

  async function getSheetRowsForProject(
    project: ProjectIdentity,
    sheetId: string,
    offset: number,
    limit: number,
    options: SheetDataOptions | null | undefined,
  ): Promise<{ rows: Row[] }> {
    const data = await getSheetRowsContract(
      project.pathId,
      Number(sheetId),
      sheetDataQuery(offset, limit, options),
    );
    return { rows: enrichRows(project, sheetId, offset, data).map((row) => (
      options?.columnIds ? { ...row, loadedColumnIds: options.columnIds } : row
    )) };
  }

  async function listSheetsForProject(project: ProjectIdentity): Promise<SheetMeta[]> {
    return listSheetsContract(project.pathId);
  }

  async function getColumnByIdForProject(
    project: ProjectIdentity,
    columnId: string,
  ): Promise<ColumnDef> {
    const sheets = await listSheetsForProject(project);
    for (const sheet of sheets) {
      for (const column of sheet.columns) {
        if (column.id === columnId) return column;
      }
    }
    throw new ApiError(404, `Column ${columnId} was not found`);
  }

  async function columnById(project: ProjectIdentity, columnId: string): Promise<ColumnDef> {
    return getColumnByIdForProject(project, columnId);
  }

  async function runColumnSetTypeAction(
    project: ProjectIdentity,
    columnId: string,
    type: ColumnType,
  ): Promise<ColumnDef> {
    const numericColumnId = Number(columnId);
    if (!Number.isInteger(numericColumnId) || numericColumnId <= 0) {
      throw new ApiError(400, `Column ${columnId} is not a valid v1 column ref`);
    }
    const params: ColumnSetTypeActionParams = {
      column_id: numericColumnId,
      type,
    };
    const spec = dependencies.v1ActionSession.registeredProjectActionSpec(
      'column.set_type', params, project.cacheId,
    );
    await dependencies.v1ActionSession.withV1ActionResult(spec, (out) => {
      dependencies.v1ActionSession.assertCompletedV1ActionResult(out, 'Column type update failed');
      const targetOutput = (out.outputs ?? []).find((candidate) => (
        candidate.kind === 'column' && String(candidate.column_id) === columnId
      ));
      if (!targetOutput) {
        throw new ApiError(500, 'Column type update did not return the target column');
      }
    });
    return columnById(project, columnId);
  }

  async function runColumnPatchAction(
    project: ProjectIdentity,
    columnId: string,
    patch: Pick<ColumnPatch, 'format'>,
  ): Promise<ColumnDef> {
    const numericColumnId = Number(columnId);
    if (!Number.isInteger(numericColumnId) || numericColumnId <= 0) {
      throw new ApiError(400, `Column ${columnId} is not a valid v1 column ref`);
    }
    if (!Object.prototype.hasOwnProperty.call(patch, 'format')) {
      throw new ApiError(400, 'Column patch requires a display format field');
    }
    const params: ColumnPatchActionParams = {
      column_id: numericColumnId,
      format: patch.format ?? null,
    };
    const spec = dependencies.v1ActionSession.registeredProjectActionSpec(
      'column.patch', params, project.cacheId,
    );
    await dependencies.v1ActionSession.withV1ActionResult(spec, (out) => {
      dependencies.v1ActionSession.assertCompletedV1ActionResult(out, 'Column patch failed');
      const targetOutput = (out.outputs ?? []).find((candidate) => (
        candidate.kind === 'column' && String(candidate.column_id) === columnId
      ));
      if (!targetOutput) {
        throw new ApiError(500, 'Column patch did not return the target column');
      }
    });
    return columnById(project, columnId);
  }

  return {
    async listSheets(): Promise<SheetMeta[]> {
      const project = captureProjectIdentity();
      return listSheetsForProject(project);
    },

    /** Set (or, with `null`, clear) the sheet-level row-title override.
     *  Resolves once persisted; callers refetch (listSheets/refreshSheets) to
     *  see it reflected. */
    async setSheetTitleColumn(sheetId: string, titleColumnId: string | null): Promise<void> {
      const project = captureProjectIdentity();
      return updateSheetContract(
        project.pathId,
        Number(sheetId),
        titleColumnId == null ? null : Number(titleColumnId),
      );
    },

    async deleteSheet(sheetId: string): Promise<DeleteSheetResult> {
      const project = captureProjectIdentity();
      return deleteSheetContract(project.pathId, Number(sheetId));
    },

    async getSheetData(
      sheetId: string,
      offset: number,
      limit: number,
      options: SheetDataOptions | null = {},
    ): Promise<SheetDataPage> {
      const project = captureProjectIdentity();
      return getSheetDataForProject(project, sheetId, offset, limit, options);
    },

    async getSheetRows(
      sheetId: string,
      offset: number,
      limit: number,
      options: SheetDataOptions | null = {},
    ): Promise<{ rows: Row[] }> {
      const project = captureProjectIdentity();
      return getSheetRowsForProject(project, sheetId, offset, limit, options);
    },

    async getColumnStats(
      sheetId: string,
      columnId: string,
      options: SheetDataOptions & { force?: boolean } = {},
    ): Promise<ColumnStats> {
      const project = captureProjectIdentity();
      return getColumnStatsContract(
        project.pathId,
        Number(sheetId),
        Number(columnId),
        {
          ...(options.force ? { force: true } : {}),
          ...(options.filter ? { filter: JSON.stringify(options.filter) } : {}),
          ...(options.sort ? { sort: JSON.stringify(options.sort) } : {}),
          ...(options.parentRowId != null ? { parent_row_id: Number(options.parentRowId) } : {}),
          ...(options.scopeRowIds != null ? { scope_row_ids: options.scopeRowIds.join(',') } : {}),
          ...(options.selection != null ? { selection: JSON.stringify(options.selection) } : {}),
          ...(options.viewId != null ? { view_id: options.viewId } : {}),
        },
      );
    },

    async locateSheetRow(
      sheetId: string,
      rowId: string,
      options: SheetDataOptions | null = {},
      pageSize = 500,
    ): Promise<SheetRowLocation> {
      const project = captureProjectIdentity();
      return locateSheetRowContract(
        project.pathId,
        Number(sheetId),
        Number(rowId),
        locateSheetRowQuery(pageSize, options),
      );
    },

    async refreshSheet(sheetId: string, confirmation?: string): Promise<SheetRefreshResult> {
      const project = captureProjectIdentity();
      const spec = dependencies.v1ActionSession.registeredProjectActionSpec(
        'sheet.refresh', { sheet_id: Number(sheetId) }, project.cacheId,
      );
      if (confirmation) {
        spec.confirmation = confirmation;
      }
      return dependencies.v1ActionSession.withV1ActionResult(spec, (out) => {
        if (out.status !== 'completed') {
          dependencies.v1ActionSession.assertCompletedV1ActionResult(
            out,
            `sheet.refresh ${out.status}`,
            out.status === 'failed' ? 500 : 409,
          );
        }
        return { status: out.status, sheetId, needsConfirmation: false };
      });
    },

    async updateColumn(columnId: string, patch: ColumnPatch): Promise<ColumnDef> {
      const project = captureProjectIdentity();
      let updated: ColumnDef | null = null;
      if (patch.type !== undefined) {
        updated = await runColumnSetTypeAction(project, columnId, patch.type);
      }
      if (patch.format !== undefined) {
        updated = await runColumnPatchAction(project, columnId, { format: patch.format });
      }
      return updated ?? columnById(project, columnId);
    },

    async addRow(
      sheetId: string,
      cells: Record<string, CellValue> = {},
    ): Promise<{ rowId: string; total: number }> {
      const project = captureProjectIdentity();
      const targetSheetId = Number(sheetId);
      if (!Number.isInteger(targetSheetId) || targetSheetId <= 0) {
        throw new ApiError(400, 'Row add target must use a positive sheet id');
      }
      const params: RowAddActionParams = {
        sheet_id: targetSheetId,
        cells: jsonCells(cells),
      };
      const spec = dependencies.v1ActionSession.registeredProjectActionSpec(
        'row.add', params, project.cacheId,
      );
      return dependencies.v1ActionSession.withV1ActionResult(spec, (out) => {
        dependencies.v1ActionSession.assertCompletedV1ActionResult(out, 'Row add failed');
        const output = (out.outputs ?? []).find((candidate) => (
          candidate.kind === 'rows' && candidate.name === 'added_rows'
        ));
        const rowId = output?.row_ids?.[0];
        const total = Number(output?.ref?.total);
        if (typeof rowId !== 'number' || rowId <= 0 || !Number.isInteger(rowId)
          || !Number.isInteger(total)) {
          throw new ApiError(500, 'Row add did not return the added row');
        }
        return { rowId: String(rowId), total };
      });
    },

    async addColumn(
      sheetId: string,
      name: string,
      options: { type?: string; position?: number | null } = {},
    ): Promise<AddColumnResult> {
      const project = captureProjectIdentity();
      const targetSheetId = Number(sheetId);
      if (!Number.isInteger(targetSheetId) || targetSheetId <= 0) {
        throw new ApiError(400, 'Column add target must use a positive sheet id');
      }
      const trimmed = name.trim();
      if (!trimmed) throw new ApiError(400, 'Column add requires a name');
      const params: ColumnAddActionParams = {
        sheet_id: targetSheetId,
        name: trimmed,
        type: options.type ?? 'text',
      };
      if (options.position != null) params.position = options.position;
      const spec = dependencies.v1ActionSession.registeredProjectActionSpec(
        'column.add', params, project.cacheId,
      );
      return dependencies.v1ActionSession.withV1ActionResult(spec, (out) => {
        dependencies.v1ActionSession.assertCompletedV1ActionResult(out, 'Column add failed');
        const output = (out.outputs ?? []).find((candidate) => candidate.kind === 'column');
        const columnId = output?.column_id;
        const ref = (output?.ref ?? {}) as { type?: unknown; position?: unknown };
        if (typeof columnId !== 'number' || columnId <= 0 || !Number.isInteger(columnId)) {
          throw new ApiError(500, 'Column add did not return the added column');
        }
        return {
          columnId: String(columnId),
          name: typeof output?.name === 'string' ? output.name : trimmed,
          type: typeof ref.type === 'string' ? ref.type : (options.type ?? 'text'),
          position: Number(ref.position),
        };
      });
    },

    async deleteRows(sheetId: string, rowIds: string[]): Promise<DeleteRowsResult> {
      const project = captureProjectIdentity();
      const targetSheetId = Number(sheetId);
      if (!Number.isInteger(targetSheetId) || targetSheetId <= 0) {
        throw new ApiError(400, 'Row delete target must use a positive sheet id');
      }
      const ids = rowIds.map((rowId) => Number(rowId));
      if (ids.length === 0 || ids.some((id) => !Number.isInteger(id) || id <= 0)) {
        throw new ApiError(400, 'Row delete requires positive row ids');
      }
      const params: RowDeleteActionParams = {
        sheet_id: targetSheetId,
        row_ids: ids,
      };
      const spec = dependencies.v1ActionSession.registeredProjectActionSpec(
        'row.delete', params, project.cacheId,
      );
      return dependencies.v1ActionSession.withV1ActionResult(spec, (out) => {
        dependencies.v1ActionSession.assertCompletedV1ActionResult(out, 'Row delete failed');
        const output = (out.outputs ?? []).find((candidate) => (
          candidate.kind === 'rows' && candidate.name === 'deleted_rows'
        ));
        const deleted = Number(output?.ref?.deleted);
        const total = Number(output?.ref?.total);
        if (!Number.isInteger(deleted) || !Number.isInteger(total)) {
          throw new ApiError(500, 'Row delete did not return a row count');
        }
        return { rowIds: (output?.row_ids ?? []).map((rowId) => String(rowId)), deleted, total };
      });
    },

    async editCells(edits: CellEdit[]): Promise<void> {
      if (edits.length === 0) return;
      const project = captureProjectIdentity();
      const actionEdits = edits.map((edit) => ({
        row_id: Number(edit.rowId),
        column_id: Number(edit.columnId),
        value: edit.value,
      }));
      const invalid = actionEdits.find((edit) => (
        !Number.isInteger(edit.row_id) || edit.row_id <= 0
        || !Number.isInteger(edit.column_id) || edit.column_id <= 0
      ));
      if (invalid) {
        throw new ApiError(400, 'Cell edit target must use positive row and column ids');
      }
      const params: CellEditActionParams = {
        edits: actionEdits.map((edit) => ({
          ...edit,
          value: jsonValueFromCell(
            edit.value,
            'Cell edit values must be JSON-compatible',
          ),
        })),
      };
      const spec = dependencies.v1ActionSession.registeredProjectActionSpec(
        'cell.edit', params, project.cacheId,
      );
      await dependencies.v1ActionSession.withV1ActionResult(spec, (out) => {
        dependencies.v1ActionSession.assertCompletedV1ActionResult(out, 'Cell edit failed');
      });
    },

    /** Replay preserve+surface: accept the fresh generated value for one
     *  pending cell as a new attributed edit op. */
    async acceptReplayValue(
      sheetId: string,
      columnId: string,
      rowId: string,
      generatedValueHash: string,
      runId: string,
    ): Promise<void> {
      const project = captureProjectIdentity();
      const params: ReplayAcceptActionParams = {
        sheet_id: Number(sheetId),
        row_id: Number(rowId),
        column_id: Number(columnId),
        generated_value_hash: generatedValueHash,
        run_id: Number(runId),
      };
      const spec = dependencies.v1ActionSession.registeredProjectActionSpec(
        'replay.accept', params, project.cacheId,
      );
      await dependencies.v1ActionSession.withV1ActionResult(spec, (out) => {
        dependencies.v1ActionSession.assertCompletedV1ActionResult(out, 'Accept newer value failed');
      });
    },

    /** Accept the fresh value for every pending cell in a column in one op. */
    async acceptReplayValuesInColumn(sheetId: string, columnId: string): Promise<void> {
      const project = captureProjectIdentity();
      const params: ReplayAcceptColumnActionParams = {
        sheet_id: Number(sheetId),
        column_id: Number(columnId),
      };
      const spec = dependencies.v1ActionSession.registeredProjectActionSpec(
        'replay.accept_column', params, project.cacheId,
      );
      await dependencies.v1ActionSession.withV1ActionResult(spec, (out) => {
        dependencies.v1ActionSession.assertCompletedV1ActionResult(
          out, 'Accept all newer values failed',
        );
      });
    },

    /** Durably keep the human edit against a specific fresh generated value. */
    async dismissReplayPending(
      sheetId: string,
      columnId: string,
      rowId: string,
      generatedValueHash: string,
      runId: string,
    ): Promise<void> {
      const project = captureProjectIdentity();
      const params: ReplayDismissActionParams = {
        sheet_id: Number(sheetId),
        row_id: Number(rowId),
        column_id: Number(columnId),
        generated_value_hash: generatedValueHash,
        run_id: Number(runId),
      };
      const spec = dependencies.v1ActionSession.registeredProjectActionSpec(
        'replay.dismiss', params, project.cacheId,
      );
      await dependencies.v1ActionSession.withV1ActionResult(spec, (out) => {
        dependencies.v1ActionSession.assertCompletedV1ActionResult(out, 'Keep edit failed');
      });
    },
  };
}
