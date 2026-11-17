import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type { SavedView, SavedViewCreateInput, SavedViewDefinitionReplaceInput, SavedViewRenameInput, ViewResolved } from './types';
interface ViewsLensesOptions { signal?: AbortSignal; headers?: HeadersInit; }
/** Structural twin of the generated contract's recursive JSON leaf. The
 *  generated module keeps its `JsonValue` private, so callers building a
 *  request body name the same shape here rather than casting into it. */
type WireJson =
  | null
  | boolean
  | number
  | string
  | WireJson[]
  | { [key: string]: WireJson };

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Carries the same JSON.stringify-compatible values as the legacy
 *  transport: object undefined keys drop, while array undefined becomes null. */
function toWireJson(value: unknown): WireJson {
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


type SavedViewListWire = HttpContractSuccessResponse<'tenant.list_views.get'>;
type SavedViewRowWire = SavedViewListWire[number];
type SavedViewCreateBody = SavedViewCreateInput;
type SavedViewPatchBody = SavedViewRenameInput;
type SavedViewDefinitionReplaceBody = SavedViewDefinitionReplaceInput;
interface ViewsLensesApi {
  listViews(sheetId?: number | null, options?: ViewsLensesOptions): Promise<SavedViewListWire>;
  createView(body: SavedViewCreateBody, options?: ViewsLensesOptions): Promise<SavedViewRowWire>;
  renameView(viewId: number, body: SavedViewPatchBody, options?: ViewsLensesOptions): Promise<SavedViewRowWire>;
  replaceViewDefinition(viewId: number, body: SavedViewDefinitionReplaceBody, options?: ViewsLensesOptions): Promise<SavedViewRowWire>;
  deleteView(viewId: number, options?: ViewsLensesOptions): Promise<unknown>;
  resolveView(viewId: number, limit?: number, offset?: number, options?: ViewsLensesOptions): Promise<ViewResolved>;
}
interface ViewsLensesDomainApi {
  listViews(sheetId?: string): Promise<SavedView[]>;
  saveView(input: SavedViewCreateInput): Promise<SavedView>;
  renameView(viewId: number, input: SavedViewRenameInput): Promise<SavedView>;
  replaceViewDefinition(viewId: number, input: SavedViewDefinitionReplaceInput): Promise<SavedView>;
  deleteView(viewId: number): Promise<void>;
  resolveView(viewId: number, opts?: { limit?: number; offset?: number }): Promise<ViewResolved>;
}

function definitionBody(body: SavedViewDefinitionReplaceInput) {
  return { selection: body.selection, presentation: toWireJsonObject({ ...body.presentation }) };
}
function toSavedView(wire: SavedViewRowWire): SavedView {
  const spec = wire.spec;
  if (!isRecord(spec.selection) || !isRecord(spec.presentation)) {
    if (wire.unavailable_reason) return { ...wire, spec: null };
    throw new Error('Saved view is missing its selection or presentation.');
  }
  return { ...wire, spec: spec as unknown as SavedView['spec'] };
}
export function createViewsLensesApi(errorFactory: (status: number, payload: unknown) => Error, projectId: string): ViewsLensesApi {
  return {
    listViews(sheetId, options = {}) {
      return httpContract('tenant.list_views.get', { pathParams: { pid: projectId }, query: { sheet_id: sheetId ?? undefined }, ...options, errorFactory });
    },
    createView(body, options = {}) {
      return httpContract('tenant.create_view.post', { pathParams: { pid: projectId }, query: {}, body: { name: body.name, ...definitionBody(body) }, ...options, errorFactory });
    },
    renameView(viewId, body, options = {}) {
      return httpContract('tenant.patch_view.patch', { pathParams: { pid: projectId, view_id: viewId }, query: {}, body, ...options, errorFactory });
    },
    replaceViewDefinition(viewId, body, options = {}) {
      return httpContract('tenant.replace_view_definition.put', { pathParams: { pid: projectId, view_id: viewId }, query: {}, body: definitionBody(body), ...options, errorFactory });
    },
    deleteView(viewId, options = {}) {
      return httpContract('tenant.delete_view_ep.delete', { pathParams: { pid: projectId, view_id: viewId }, query: {}, ...options, errorFactory });
    },
    resolveView(viewId, limit, offset, options = {}) {
      return httpContract('tenant.resolve_view.get', { pathParams: { pid: projectId, view_id: viewId }, query: { limit, offset }, ...options, errorFactory });
    },
  };
}
export function createViewsLensesDomainApi(errorFactory: (status: number, payload: unknown) => Error, projectId: string): ViewsLensesDomainApi {
  const transport = createViewsLensesApi(errorFactory, projectId);
  return {
    async listViews(sheetId) { return (await transport.listViews(sheetId ? Number(sheetId) : null)).map(toSavedView); },
    async saveView(input) { return toSavedView(await transport.createView(input)); },
    async renameView(id, input) { return toSavedView(await transport.renameView(id, input)); },
    async replaceViewDefinition(id, input) { return toSavedView(await transport.replaceViewDefinition(id, input)); },
    async deleteView(id) { await transport.deleteView(id); },
    resolveView(id, opts) { return transport.resolveView(id, opts?.limit, opts?.offset); },
  };
}
