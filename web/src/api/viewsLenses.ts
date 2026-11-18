import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type { View, ViewCreateInput, ViewDefinitionReplaceInput, ViewRenameInput, ViewResolved } from './types';
import { isRecord } from './wireValues';

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

type ViewListWire = HttpContractSuccessResponse<'tenant.list_views.get'>;
type ViewRowWire = ViewListWire[number];
function definitionBody(body: ViewDefinitionReplaceInput) {
  return { selection: body.selection, presentation: toWireJsonObject({ ...body.presentation }) };
}
function toView(wire: ViewRowWire): View {
  const spec = wire.spec;
  if (!isRecord(spec.selection) || !isRecord(spec.presentation)) {
    if (wire.unavailable_reason) return { ...wire, spec: null };
    throw new Error('View is missing its selection or presentation.');
  }
  return { ...wire, spec: spec as unknown as View['spec'] };
}
export function createViewsApi(projectId: string) {
  return {
    async listViews(sheetId?: string) {
      return httpContract('tenant.list_views.get', {
        pathParams: { pid: projectId },
        query: { sheet_id: sheetId ? Number(sheetId) : undefined },
      }, (wire) => wire.map(toView));
    },
    async saveView(input: ViewCreateInput) {
      return httpContract('tenant.create_view.post', {
        pathParams: { pid: projectId },
        body: { name: input.name, ...definitionBody(input) },
      }, toView);
    },
    async renameView(viewId: number, input: ViewRenameInput) {
      return httpContract('tenant.patch_view.patch', {
        pathParams: { pid: projectId, view_id: viewId },
        body: input,
      }, toView);
    },
    async replaceViewDefinition(viewId: number, input: ViewDefinitionReplaceInput) {
      return httpContract('tenant.replace_view_definition.put', {
        pathParams: { pid: projectId, view_id: viewId },
        body: definitionBody(input),
      }, toView);
    },
    async deleteView(viewId: number): Promise<void> {
      await httpContract('tenant.delete_view_ep.delete', {
        pathParams: { pid: projectId, view_id: viewId },
      });
    },
    resolveView(viewId: number, opts?: { limit?: number; offset?: number }): Promise<ViewResolved> {
      return httpContract('tenant.resolve_view.get', {
        pathParams: { pid: projectId, view_id: viewId },
        query: { limit: opts?.limit, offset: opts?.offset },
      });
    },
  };
}
