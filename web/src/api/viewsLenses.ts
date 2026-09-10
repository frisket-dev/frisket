import type { HttpContractOperationMap } from "../generated/openHttpContracts";
import { httpContract, type HttpContractSuccessResponse } from "./httpContract";
import type {
  Lens,
  LensResolved,
  LensSaveInput,
  SavedView,
  SavedViewCreateInput,
  SavedViewDefinitionReplaceInput,
  SavedViewRenameInput,
} from "./types";

export interface ViewsLensesOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

type ContractRequestBody<Id extends keyof HttpContractOperationMap> = Exclude<
  HttpContractOperationMap[Id]["request"],
  undefined
>;

/** Structural twin of the generated contract's recursive JSON leaf. The
 *  generated module keeps its `JsonValue` private, so callers building a
 *  request body name the same shape here rather than casting into it. */
export type WireJson =
  | null
  | boolean
  | number
  | string
  | WireJson[]
  | { [key: string]: WireJson };

export type SavedViewListWire =
  HttpContractSuccessResponse<"tenant.list_views.get">;
export type SavedViewRowWire = SavedViewListWire[number];
export type SavedViewCreatedWire =
  HttpContractSuccessResponse<"tenant.create_view.post">;
export type SavedViewPatchedWire =
  HttpContractSuccessResponse<"tenant.patch_view.patch">;
export type SavedViewDefinitionReplacedWire =
  HttpContractSuccessResponse<"tenant.replace_view_definition.put">;
export type SavedViewDeletedWire =
  HttpContractSuccessResponse<"tenant.delete_view_ep.delete">;
export type SavedLensListWire =
  HttpContractSuccessResponse<"tenant.list_lenses.get">;
export type SavedLensRowWire = SavedLensListWire[number];
export type SavedLensCreatedWire =
  HttpContractSuccessResponse<"tenant.create_lens.post">;
export type SavedLensResolvedWire =
  HttpContractSuccessResponse<"tenant.resolve_lens.get">;

export type SavedViewCreateBody =
  ContractRequestBody<"tenant.create_view.post">;
export type SavedViewPatchBody = ContractRequestBody<"tenant.patch_view.patch">;
export type SavedViewDefinitionReplaceBody =
  ContractRequestBody<"tenant.replace_view_definition.put">;
export type SavedLensCreateBody =
  ContractRequestBody<"tenant.create_lens.post">;

export interface ViewsLensesApi {
  listViews(
    sheetId?: number | null,
    options?: ViewsLensesOptions,
  ): Promise<SavedViewListWire>;
  createView(
    body: SavedViewCreateBody,
    options?: ViewsLensesOptions,
  ): Promise<SavedViewCreatedWire>;
  renameView(
    viewId: number,
    body: SavedViewPatchBody,
    options?: ViewsLensesOptions,
  ): Promise<SavedViewPatchedWire>;
  replaceViewDefinition(
    viewId: number,
    body: SavedViewDefinitionReplaceBody,
    options?: ViewsLensesOptions,
  ): Promise<SavedViewDefinitionReplacedWire>;
  deleteView(
    viewId: number,
    options?: ViewsLensesOptions,
  ): Promise<SavedViewDeletedWire>;
  listLenses(
    sheetId?: number | null,
    options?: ViewsLensesOptions,
  ): Promise<SavedLensListWire>;
  createLens(
    body: SavedLensCreateBody,
    options?: ViewsLensesOptions,
  ): Promise<SavedLensCreatedWire>;
  /** Event-triggered only: the resolve is issued by an explicit lens open,
   *  never prefetched, cached, polled, or retried by this transport. */
  resolveLens(
    lensId: number,
    limit?: number | null,
    offset?: number | null,
    options?: ViewsLensesOptions,
  ): Promise<SavedLensResolvedWire>;
}

/** The feature-facing port. The generated transport stays private to this
 *  module so real.ts only composes the domain API and delegates to it. */
export interface ViewsLensesDomainApi {
  listViews(sheetId?: string): Promise<SavedView[]>;
  saveView(input: SavedViewCreateInput): Promise<SavedView>;
  renameView(viewId: number, input: SavedViewRenameInput): Promise<SavedView>;
  replaceViewDefinition(
    viewId: number,
    input: SavedViewDefinitionReplaceInput,
  ): Promise<SavedView>;
  deleteView(viewId: number): Promise<void>;
  listLenses(sheetId?: number | string): Promise<Lens[]>;
  saveLens(input: LensSaveInput): Promise<Lens>;
  resolveLens(
    lensId: number,
    opts?: { limit?: number; offset?: number },
  ): Promise<LensResolved>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Carries the same JSON.stringify-compatible values as the legacy
 *  transport: object undefined keys drop, while array undefined becomes null. */
export function toWireJson(value: unknown): WireJson {
  if (
    value === null
    || typeof value === "boolean"
    || typeof value === "number"
    || typeof value === "string"
  ) {
    return value;
  }
  if (Array.isArray(value)) return value.map(toWireJson);
  if (isRecord(value)) return toWireJsonObject(value);
  return null;
}

export function toWireJsonObject(value: Record<string, unknown>): { [key: string]: WireJson } {
  const out: { [key: string]: WireJson } = {};
  for (const [key, child] of Object.entries(value)) {
    if (child !== undefined) out[key] = toWireJson(child);
  }
  return out;
}

function toWireViewCreate(input: SavedViewCreateInput): SavedViewCreateBody {
  return {
    name: input.name,
    sheet_id: Number(input.sheetId),
    filter: toWireJsonObject(input.filter),
    sort: input.sort === null ? null : input.sort.map(toWireJson),
    columns: input.columns === null ? null : input.columns.map(toWireJson),
    column_groups: input.column_groups === null
      ? null
      : input.column_groups.map(toWireJson),
  };
}

function toWireViewRename(input: SavedViewRenameInput): SavedViewPatchBody {
  return { name: input.name };
}

function toWireViewDefinitionReplace(
  input: SavedViewDefinitionReplaceInput,
): SavedViewDefinitionReplaceBody {
  return {
    filter: toWireJsonObject(input.filter),
    sort: input.sort === null ? null : input.sort.map(toWireJson),
    columns: input.columns === null ? null : input.columns.map(toWireJson),
    column_groups: input.column_groups === null
      ? null
      : input.column_groups.map(toWireJson),
  };
}

/** The generated Saved View row keeps `spec` as generic JSON. Decode that
 * one generic seam into the published non-null object-filter contract.
 * Filter interpretation stays with the existing grid normalizer. */
function toSavedView(
  wire: SavedViewRowWire
    | SavedViewCreatedWire
    | SavedViewPatchedWire
    | SavedViewDefinitionReplacedWire,
): SavedView {
  if (!isRecord(wire.spec)) throw new Error("Saved View spec must be an object");
  if (!isRecord(wire.spec.filter)) {
    throw new Error("Saved View filter must be an object");
  }
  const spec: SavedView["spec"] = {
    ...wire.spec,
    filter: wire.spec.filter,
  };
  return {
    id: wire.id,
    name: wire.name,
    sheet_id: wire.sheet_id,
    spec,
    op_id: wire.op_id,
  };
}

function toWireLensCreate(input: LensSaveInput): SavedLensCreateBody {
  const out: SavedLensCreateBody = {
    name: input.name,
    query: toWireJsonObject(input.query),
  };
  if (input.presentation) out.presentation = toWireJsonObject(input.presentation);
  return out;
}

function toLens(wire: SavedLensRowWire): Lens {
  return {
    id: wire.id,
    name: wire.name,
    sheetId: wire.sheet_id ?? null,
    spec: wire.spec ?? {},
    opId: wire.op_id ?? null,
    createdAt: wire.created_at ?? null,
    updatedAt: wire.updated_at ?? null,
  };
}

export function createViewsLensesApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): ViewsLensesApi {
  return {
    listViews(sheetId, options = {}) {
      return httpContract(
        "tenant.list_views.get",
        {
          pathParams: { pid: projectId },
          query: { sheet_id: sheetId ?? undefined },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    createView(body, options = {}) {
      return httpContract(
        "tenant.create_view.post",
        {
          pathParams: { pid: projectId },
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    renameView(viewId, body, options = {}) {
      return httpContract(
        "tenant.patch_view.patch",
        {
          pathParams: { pid: projectId, view_id: viewId },
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    replaceViewDefinition(viewId, body, options = {}) {
      return httpContract(
        "tenant.replace_view_definition.put",
        {
          pathParams: { pid: projectId, view_id: viewId },
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    deleteView(viewId, options = {}) {
      return httpContract(
        "tenant.delete_view_ep.delete",
        {
          pathParams: { pid: projectId, view_id: viewId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    listLenses(sheetId, options = {}) {
      return httpContract(
        "tenant.list_lenses.get",
        {
          pathParams: { pid: projectId },
          query: { sheet_id: sheetId ?? undefined },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    createLens(body, options = {}) {
      return httpContract(
        "tenant.create_lens.post",
        {
          pathParams: { pid: projectId },
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    resolveLens(lensId, limit, offset, options = {}) {
      return httpContract(
        "tenant.resolve_lens.get",
        {
          pathParams: { pid: projectId, lens_id: lensId },
          query: { limit: limit ?? undefined, offset: offset ?? undefined },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

  };
}

export function createViewsLensesDomainApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): ViewsLensesDomainApi {
  const transport = createViewsLensesApi(errorFactory, projectId);
  return {
    async listViews(sheetId) {
      return (await transport.listViews(sheetId ? Number(sheetId) : null)).map(
        toSavedView,
      );
    },

    async saveView(input) {
      return toSavedView(await transport.createView(toWireViewCreate(input)));
    },

    async renameView(viewId, input) {
      return toSavedView(
        await transport.renameView(viewId, toWireViewRename(input)),
      );
    },

    async replaceViewDefinition(viewId, input) {
      return toSavedView(
        await transport.replaceViewDefinition(
          viewId,
          toWireViewDefinitionReplace(input),
        ),
      );
    },

    async deleteView(viewId) {
      await transport.deleteView(viewId);
    },

    async listLenses(sheetId) {
      const wire = await transport.listLenses(sheetId == null ? null : Number(sheetId));
      return wire.map(toLens);
    },

    async saveLens(input) {
      return toLens(await transport.createLens(toWireLensCreate(input)));
    },

    async resolveLens(lensId, opts) {
      const wire = await transport.resolveLens(
        lensId,
        opts?.limit ?? null,
        opts?.offset ?? null,
      );
      const rawScores = wire.scores ?? {};
      const scores: LensResolved["scores"] = {};
      for (const [rowId, score] of Object.entries(rawScores)) {
        scores[rowId] = { distance: score?.distance ?? null, score: score?.score ?? null };
      }
      const rowIds = wire.row_ids ?? [];
      return {
        lensId: wire.lens_id,
        schemaVersion: wire.schema_version,
        sheetId: wire.sheet_id ?? null,
        queryHash: wire.query_hash ?? null,
        rowIds,
        rows: rowIds.map((rowId) => ({
          rowId,
          distance: scores[String(rowId)]?.distance ?? null,
          score: scores[String(rowId)]?.score ?? null,
        })),
        scores,
        rowCount: wire.row_count ?? rowIds.length,
        total: wire.total ?? rowIds.length,
        offset: wire.offset ?? 0,
        limit: wire.limit ?? rowIds.length,
      };
    },
  };
}
