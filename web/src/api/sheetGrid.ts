import { ApiError } from './contractErrors';
import type { GeneratedActionParams } from '../generated/actionTypes';
import {
  getColumnStatsContract,
  getSheetDataContract,
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
} from './types';
import type { V1ActionSession } from './v1ActionSession';

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type ColumnPatchActionParams = GeneratedActionParams['column.patch'];
type ColumnAddActionParams = GeneratedActionParams['column.add'];
type ColumnSetTypeActionParams = GeneratedActionParams['column.set_type'];
type CellEditActionParams = GeneratedActionParams['cell.edit'];
type RowAddActionParams = GeneratedActionParams['row.add'];
type RowDeleteActionParams = GeneratedActionParams['row.delete'];
type ReplayAcceptActionParams = GeneratedActionParams['replay.accept'];
type ReplayAcceptColumnActionParams = GeneratedActionParams['replay.accept_column'];
type ReplayDismissActionParams = GeneratedActionParams['replay.dismiss'];

export interface SheetGridColumnRunInfo {
  actionName: string;
  prompt: string;
  model: string;
}

export interface SheetGridDependencies {
  columnRunInfo(
    projectId: string,
    sheetId: string,
    column: { id: string; name: string },
  ): SheetGridColumnRunInfo | undefined;
  /** RealApi remains the action-session composition root. */
  v1ActionSession: Pick<
    V1ActionSession,
    | 'v1ActionSpec'
    | 'registeredProjectActionSpec'
    | 'withV1ActionResult'
    | 'assertCompletedV1ActionResult'
  >;
}

export interface SheetGridDomainApi {
  listSheets(): Promise<SheetMeta[]>;
  setSheetTitleColumn(
    sheetId: string,
    titleColumnId: string | null,
  ): Promise<void>;
  deleteSheet(sheetId: string): Promise<DeleteSheetResult>;
  getSheetData(
    sheetId: string,
    offset: number,
    limit: number,
    options?: SheetDataOptions | null,
  ): Promise<SheetDataPage>;
  getColumnStats(
    sheetId: string,
    columnId: string,
    options?: { force?: boolean },
  ): Promise<ColumnStats>;
  locateSheetRow(
    sheetId: string,
    rowId: string,
    options?: SheetDataOptions | null,
    pageSize?: number,
  ): Promise<SheetRowLocation>;
  getColumnById(columnId: string, explicitProjectPathId?: string): Promise<ColumnDef>;
  refreshSheet(
    sheetId: string,
    confirmation?: string,
  ): Promise<SheetRefreshResult>;
  updateColumn(columnId: string, patch: ColumnPatch): Promise<ColumnDef>;
  addRow(sheetId: string, cells?: Record<string, CellValue>): Promise<{ rowId: string; total: number }>;
  addColumn(
    sheetId: string,
    name: string,
    options?: { type?: string; position?: number | null },
  ): Promise<AddColumnResult>;
  deleteRows(sheetId: string, rowIds: string[]): Promise<DeleteRowsResult>;
  editCells(edits: CellEdit[]): Promise<void>;
  acceptReplayValue(
    sheetId: string,
    columnId: string,
    rowId: string,
    generatedValueHash: string,
    runId: string,
  ): Promise<void>;
  acceptReplayValuesInColumn(sheetId: string, columnId: string): Promise<void>;
  dismissReplayPending(
    sheetId: string,
    columnId: string,
    rowId: string,
    generatedValueHash: string,
    runId: string,
  ): Promise<void>;
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

export function createSheetGridDomainApi(
  errorFactory: ContractErrorFactory,
  dependencies: SheetGridDependencies,
  projectId: string,
): SheetGridDomainApi {
  const captureProjectIdentity = (explicitProjectPathId?: string): ProjectIdentity => {
    const cacheId = explicitProjectPathId ?? projectId;
    return { cacheId, pathId: cacheId };
  };
  function enrichAiColumn<Column extends ColumnDef>(
    project: ProjectIdentity,
    sheetId: string,
    column: Column,
  ): Column {
    if (!column.ai) return column;
    const info = dependencies.columnRunInfo(project.cacheId, sheetId, column);
    if (!info) return column;
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
      errorFactory,
    );
    const columns = data.columns.map((column) =>
      enrichAiColumn(project, sheetId, column),
    );
    const columnById = new Map(columns.map((column) => [column.id, column]));
    const rows = data.rows.map((row, index) => ({
      ...row,
      index: offset + index,
      provenance: Object.fromEntries(
        Object.entries(row.provenance).map(([columnId, provenance]) => {
          const column = columnById.get(columnId);
          const info = column
            ? dependencies.columnRunInfo(project.cacheId, sheetId, column)
            : undefined;
          return [columnId, {
            ...provenance,
            model: info?.model ?? provenance.model,
            actionName: info?.actionName ?? provenance.actionName,
          }];
        }),
      ),
    }));
    return { rows, total: data.total, columns };
  }

  async function listSheetsForProject(project: ProjectIdentity): Promise<SheetMeta[]> {
    const sheets = await listSheetsContract(project.pathId, errorFactory);
    return Promise.all(sheets.map(async (sheet) => {
      const data = await getSheetDataForProject(project, sheet.id, 0, 0, {});
      return { ...sheet, columns: data.columns };
    }));
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
    listSheets() {
      const project = captureProjectIdentity();
      return listSheetsForProject(project);
    },

    setSheetTitleColumn(sheetId, titleColumnId) {
      const project = captureProjectIdentity();
      return updateSheetContract(
        project.pathId,
        Number(sheetId),
        titleColumnId == null ? null : Number(titleColumnId),
        errorFactory,
      );
    },

    deleteSheet(sheetId) {
      const project = captureProjectIdentity();
      return deleteSheetContract(project.pathId, Number(sheetId), errorFactory);
    },

    getSheetData(sheetId, offset, limit, options = {}) {
      const project = captureProjectIdentity();
      return getSheetDataForProject(project, sheetId, offset, limit, options);
    },

    getColumnStats(sheetId, columnId, options = {}) {
      const project = captureProjectIdentity();
      return getColumnStatsContract(
        project.pathId,
        Number(sheetId),
        Number(columnId),
        options.force === true,
        errorFactory,
      );
    },

    locateSheetRow(sheetId, rowId, options = {}, pageSize = 500) {
      const project = captureProjectIdentity();
      return locateSheetRowContract(
        project.pathId,
        Number(sheetId),
        Number(rowId),
        locateSheetRowQuery(pageSize, options),
        errorFactory,
      );
    },

    getColumnById(columnId, explicitProjectPathId) {
      const project = captureProjectIdentity(explicitProjectPathId);
      return getColumnByIdForProject(project, columnId);
    },

    async refreshSheet(sheetId, confirmation) {
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

    async updateColumn(columnId, patch) {
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

    async addRow(sheetId, cells = {}) {
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

    async addColumn(sheetId, name, options = {}) {
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

    async deleteRows(sheetId, rowIds) {
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

    async editCells(edits) {
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

    async acceptReplayValue(sheetId, columnId, rowId, generatedValueHash, runId) {
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
        dependencies.v1ActionSession.assertCompletedV1ActionResult(out, 'Accept regenerated value failed');
      });
    },

    async acceptReplayValuesInColumn(sheetId, columnId) {
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
          out, 'Accept all regenerated values failed',
        );
      });
    },

    async dismissReplayPending(sheetId, columnId, rowId, generatedValueHash, runId) {
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
