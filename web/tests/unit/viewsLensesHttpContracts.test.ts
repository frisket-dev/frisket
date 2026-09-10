import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("apache-arrow", () => ({ tableFromIPC: vi.fn() }));
vi.mock("pluralize", () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));


const viewRow = {
  id: 4,
  name: "Open rows",
  sheet_id: 3,
  spec: { filter: { q: "x" }, sort: [["a", "asc"]] },
  op_id: 11,
  created_at: "2026-08-10 00:00:00",
  updated_at: "2026-08-10 00:01:00",
};

const expectedSavedView = {
  id: viewRow.id,
  name: viewRow.name,
  sheet_id: viewRow.sheet_id,
  spec: viewRow.spec,
  op_id: viewRow.op_id,
};

const lensRow = {
  id: 5,
  name: "Similar rows",
  sheet_id: 3,
  spec: {
    schema_version: "frisket.lens.v1",
    query: { schema_version: "frisket.query.v1", kind: "sheet.filter" },
    presentation: { columns: ["a"] },
  },
  op_id: 12,
  created_at: "2026-08-10 00:02:00",
  updated_at: "2026-08-10 00:03:00",
};

const resolveFixture = {
  lens_id: 5,
  schema_version: "frisket.query_preview.v1",
  query: { schema_version: "frisket.query.v1", kind: "sheet.filter" },
  query_hash: "sha256:abc",
  sheet_id: 3,
  row_ids: [7, 8],
  row_count: 2,
  total: 9,
  offset: 1,
  limit: 50,
  evaluator: { kind: "frisket.querysets.sheet_filter", version: "v1" },
  scores: {
    "7": { distance: 0.25, score: 0.75 },
    "8": { distance: null, score: null },
  },
};

function jsonResponse(
  payload: unknown,
  status = 200,
  contentType = "application/json",
): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": contentType },
  });
}

interface Options {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

interface ViewsLensesTransport {
  listViews(sheetId?: number | null, options?: Options): Promise<unknown>;
  createView(body: Record<string, unknown>, options?: Options): Promise<unknown>;
  renameView(
    viewId: number,
    body: Record<string, unknown>,
    options?: Options,
  ): Promise<unknown>;
  replaceViewDefinition(
    viewId: number,
    body: Record<string, unknown>,
    options?: Options,
  ): Promise<unknown>;
  deleteView(viewId: number, options?: Options): Promise<unknown>;
  listLenses(sheetId?: number | null, options?: Options): Promise<unknown>;
  createLens(body: Record<string, unknown>, options?: Options): Promise<unknown>;
  resolveLens(
    lensId: number,
    limit?: number | null,
    offset?: number | null,
    options?: Options,
  ): Promise<unknown>;
}

interface ViewsLensesModule {
  createViewsLensesApi(
    factory: (status: number, payload: unknown) => Error,
  ): ViewsLensesTransport;
  createViewsLensesDomainApi?: (
    factory: (status: number, payload: unknown) => Error,
  ) => ViewsLensesDomainTransport;
}

interface ViewsLensesDomainTransport {
  listViews(sheetId?: string): Promise<unknown>;
  saveView(body: {
    name: string;
    sheetId: string;
    filter: Record<string, unknown>;
    sort: unknown[] | null;
    columns: string[] | null;
    column_groups: unknown[] | null;
  }): Promise<unknown>;
  renameView(viewId: number, body: { name: string }): Promise<unknown>;
  replaceViewDefinition(
    viewId: number,
    body: {
      filter: Record<string, unknown>;
      sort: unknown[] | null;
      columns: string[] | null;
      column_groups: unknown[] | null;
    },
  ): Promise<unknown>;
  saveLens(body: {
    name: string;
    query: Record<string, unknown>;
  }): Promise<unknown>;
  resolveLens(
    lensId: number,
    options?: { limit?: number; offset?: number },
  ): Promise<unknown>;
}

async function viewsLensesModule(): Promise<ViewsLensesModule | undefined> {
  const path = "../../src/api/viewsLenses";
  return import(/* @vite-ignore */ path).catch(() => undefined) as Promise<
    ViewsLensesModule | undefined
  >;
}

class MappedContractError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`mapped view/lens error ${status}`);
    this.name = "MappedContractError";
    this.status = status;
    this.payload = payload;
  }
}

/** The eight browser operations, by adapter method. */
const methodOperationIds: Record<string, string> = {
  listViews: "tenant.list_views.get",
  createView: "tenant.create_view.post",
  renameView: "tenant.patch_view.patch",
  replaceViewDefinition: "tenant.replace_view_definition.put",
  deleteView: "tenant.delete_view_ep.delete",
  listLenses: "tenant.list_lenses.get",
  createLens: "tenant.create_lens.post",
  resolveLens: "tenant.resolve_lens.get",
};

/** The realApi methods that must ride the generated transport, and the
 *  operation each one is expected to invoke. */
const realApiOperationIds: Record<string, string> = {
  listViews: "tenant.list_views.get",
  saveView: "tenant.create_view.post",
  renameView: "tenant.patch_view.patch",
  replaceViewDefinition: "tenant.replace_view_definition.put",
  deleteView: "tenant.delete_view_ep.delete",
  listLenses: "tenant.list_lenses.get",
  saveLens: "tenant.create_lens.post",
  resolveLens: "tenant.resolve_lens.get",
};

interface TransportCall {
  operationId: string;
  options: { pathParams?: unknown; query?: unknown; body?: unknown };
}

/** Install a scoped mock of the generated transport, run `body` against a
 *  FRESH module registry, then restore. Everything the code under test needs
 *  must be imported INSIDE `body` so it comes from that same registry. */
async function withMockedTransport<T>(
  respond: (operationId: string) => unknown,
  body: (calls: TransportCall[], fetchSpy: ReturnType<typeof vi.fn>) => Promise<T>,
): Promise<T> {
  const calls: TransportCall[] = [];
  vi.resetModules();
  vi.doMock("../../src/api/httpContract", () => ({
    httpContract: (operationId: string, options: TransportCall["options"]) => {
      calls.push({ operationId, options });
      return Promise.resolve(respond(operationId));
    },
  }));
  const fetchSpy = vi.fn(async () => jsonResponse({}));
  vi.stubGlobal("fetch", fetchSpy);
  try {
    return await body(calls, fetchSpy);
  } finally {
    vi.doUnmock("../../src/api/httpContract");
    vi.unstubAllGlobals();
    vi.resetModules();
  }
}

/** Wire payload the transport hands back for each operation. */
function transportResponse(operationId: string): unknown {
  if (operationId === "tenant.list_views.get") return [viewRow];
  if (operationId === "tenant.create_view.post") return viewRow;
  if (operationId === "tenant.patch_view.patch") return viewRow;
  if (operationId === "tenant.replace_view_definition.put") return viewRow;
  if (operationId === "tenant.list_lenses.get") return [lensRow];
  if (operationId === "tenant.create_lens.post") return lensRow;
  if (operationId === "tenant.resolve_lens.get") return resolveFixture;
  return { ok: true, deleted: 4 };
}

afterEach(() => vi.unstubAllGlobals());

describe("saved view and saved lens generated HTTP transport", () => {
  it("owns exactly the eight operations and preserves path, query, body, and options", async () => {
    const module = await viewsLensesModule();
    expect(
      module,
      "INTENDED_F5_RED: viewsLenses generated adapter must exist",
    ).toBeDefined();
    if (!module) return;
    const api = module.createViewsLensesApi(
      (status, payload) => new MappedContractError(status, payload),
      "view-lens-contract",
    );
    expect(Object.keys(api).sort()).toEqual([
      "createLens",
      "createView",
      "deleteView",
      "listLenses",
      "listViews",
      "renameView",
      "replaceViewDefinition",
      "resolveLens",
    ]);

    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses: unknown[] = [
      [viewRow],
      [viewRow],
      viewRow,
      viewRow,
      viewRow,
      { ok: true, deleted: 4 },
      [lensRow],
      [lensRow],
      lensRow,
      resolveFixture,
      resolveFixture,
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        requests.push({ input, init });
        return jsonResponse(responses.shift());
      }),
    );
    const signal = new AbortController().signal;
    const options = {
      signal,
      headers: { Authorization: "Bearer view-lens", "X-Trace-Id": "f5" },
    };

    await api.listViews(undefined, options);
    await api.listViews(3, options);
    await api.createView(
      {
        name: "V",
        sheet_id: 3,
        filter: { q: "x" },
        sort: null,
        columns: null,
        column_groups: null,
      },
      options,
    );
    await api.renameView(4, { name: "V2" }, options);
    await api.replaceViewDefinition(
      4,
      { filter: { q: "updated" }, sort: null, columns: null, column_groups: null },
      options,
    );
    await api.deleteView(4, options);
    await api.listLenses(null, options);
    await api.listLenses(3, options);
    await api.createLens({ name: "L", query: { kind: "filter" } }, options);
    await api.resolveLens(5, undefined, undefined, options);
    await api.resolveLens(5, 5000, 12, options);

    expect(
      requests.map((request) => [request.input, request.init?.method]),
    ).toEqual([
      ["/api/projects/view-lens-contract/views", "GET"],
      ["/api/projects/view-lens-contract/views?sheet_id=3", "GET"],
      ["/api/projects/view-lens-contract/views", "POST"],
      ["/api/projects/view-lens-contract/views/4", "PATCH"],
      ["/api/projects/view-lens-contract/views/4/definition", "PUT"],
      ["/api/projects/view-lens-contract/views/4", "DELETE"],
      ["/api/projects/view-lens-contract/lenses", "GET"],
      ["/api/projects/view-lens-contract/lenses?sheet_id=3", "GET"],
      ["/api/projects/view-lens-contract/lenses", "POST"],
      ["/api/projects/view-lens-contract/lenses/5/resolve", "GET"],
      [
        "/api/projects/view-lens-contract/lenses/5/resolve?limit=5000&offset=12",
        "GET",
      ],
    ]);
    // Rename has only its name; create/replacement carry an explicit complete
    // definition, including intentional null presentation fields.
    expect(requests.map((request) => request.init?.body)).toEqual([
      undefined,
      undefined,
      JSON.stringify({
        name: "V",
        sheet_id: 3,
        filter: { q: "x" },
        sort: null,
        columns: null,
        column_groups: null,
      }),
      JSON.stringify({ name: "V2" }),
      JSON.stringify({
        filter: { q: "updated" },
        sort: null,
        columns: null,
        column_groups: null,
      }),
      undefined,
      undefined,
      undefined,
      JSON.stringify({ name: "L", query: { kind: "filter" } }),
      undefined,
      undefined,
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get("authorization")).toBe("Bearer view-lens");
      expect(headers.get("x-trace-id")).toBe("f5");
    }
  });

  it("keeps the preserved realApi domain mapping for views, lenses, and resolve", async () => {
    const real = await import("../../src/api/real");
    const projectApi = real.createProjectApi("view-lens-domain");
    const requests: Array<RequestInfo | URL> = [];
    const responses: unknown[] = [
      [viewRow],
      viewRow,
      viewRow,
      viewRow,
      { ok: true, deleted: 4 },
      [lensRow],
      lensRow,
      resolveFixture,
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        requests.push(input);
        return jsonResponse(responses.shift());
      }),
    );
    const listedViews = await projectApi.listViews("3");
    const savedView = await projectApi.saveView({
      name: "V",
      sheetId: "3",
      filter: { q: "x" },
      sort: null,
      columns: null,
      column_groups: null,
    });
    const renamedView = await projectApi.renameView(4, { name: "V2" });
    const replacedView = await projectApi.replaceViewDefinition(4, {
      filter: { q: "updated" },
      sort: null,
      columns: null,
      column_groups: null,
    });
    const deletedView = await projectApi.deleteView(4);
    const listedLenses = await projectApi.listLenses(3);
    const savedLens = await projectApi.saveLens({
      name: "L",
      query: { kind: "filter" },
    });
    const resolved = await projectApi.resolveLens(5, { limit: 5000, offset: 12 });

    // SavedView exposes its five product fields after the generic wire spec
    // is checked for the required object filter.
    expect(listedViews).toEqual([expectedSavedView]);
    expect(savedView).toEqual(expectedSavedView);
    expect(renamedView).toEqual(expectedSavedView);
    expect(replacedView).toEqual(expectedSavedView);
    expect(deletedView).toBeUndefined();

    // Lens keeps its seven camelCase fields.
    const expectedLens = {
      id: 5,
      name: "Similar rows",
      sheetId: 3,
      spec: lensRow.spec,
      opId: 12,
      createdAt: "2026-08-10 00:02:00",
      updatedAt: "2026-08-10 00:03:00",
    };
    expect(listedLenses).toEqual([expectedLens]);
    expect(savedLens).toEqual(expectedLens);

    // LensResolved keeps its nullable compatibility and derived rows.
    expect(resolved).toEqual({
      lensId: 5,
      schemaVersion: "frisket.query_preview.v1",
      sheetId: 3,
      queryHash: "sha256:abc",
      rowIds: [7, 8],
      rows: [
        { rowId: 7, distance: 0.25, score: 0.75 },
        { rowId: 8, distance: null, score: null },
      ],
      scores: {
        "7": { distance: 0.25, score: 0.75 },
        "8": { distance: null, score: null },
      },
      rowCount: 2,
      total: 9,
      offset: 1,
      limit: 50,
    });

    expect(requests).toEqual([
      "/api/projects/view-lens-domain/views?sheet_id=3",
      "/api/projects/view-lens-domain/views",
      "/api/projects/view-lens-domain/views/4",
      "/api/projects/view-lens-domain/views/4/definition",
      "/api/projects/view-lens-domain/views/4",
      "/api/projects/view-lens-domain/lenses?sheet_id=3",
      "/api/projects/view-lens-domain/lenses",
      "/api/projects/view-lens-domain/lenses/5/resolve?limit=5000&offset=12",
    ]);
  });

  it("maps direct view/lens domain inputs and outputs through recursive JSON", async () => {
    await withMockedTransport(transportResponse, async (calls) => {
      const fresh = (await import("../../src/api/viewsLenses").catch(
        () => undefined,
      )) as ViewsLensesModule | undefined;
      expect(fresh?.createViewsLensesDomainApi).toBeDefined();
      if (!fresh?.createViewsLensesDomainApi) return;
      const api = fresh.createViewsLensesDomainApi(
        (status, payload) => new MappedContractError(status, payload),
        "view-lens-domain-direct",
      );
      const savedView = await api.saveView({
        name: "V",
        sheetId: "3",
        filter: { nested: { keep: true, dropped: undefined } },
        sort: null,
        columns: null,
        column_groups: null,
      });
      const renamedView = await api.renameView(4, { name: "Renamed" });
      const replacedView = await api.replaceViewDefinition(4, {
        filter: { nested: { replacement: true, dropped: undefined } },
        sort: null,
        columns: null,
        column_groups: null,
      });
      const savedLens = await api.saveLens({
        name: "L",
        query: {
          nested: { keep: true, dropped: undefined },
          values: [1, undefined, { alsoDropped: undefined, kept: "yes" }],
        },
      });
      const resolved = await api.resolveLens(5, { limit: 5000, offset: 12 });

      expect(calls.map((call) => [call.operationId, call.options.body])).toEqual([
        [
          "tenant.create_view.post",
          {
            name: "V",
            sheet_id: 3,
            filter: { nested: { keep: true } },
            sort: null,
            columns: null,
            column_groups: null,
          },
        ],
        ["tenant.patch_view.patch", { name: "Renamed" }],
        [
          "tenant.replace_view_definition.put",
          {
            filter: { nested: { replacement: true } },
            sort: null,
            columns: null,
            column_groups: null,
          },
        ],
        [
          "tenant.create_lens.post",
          {
            name: "L",
            query: {
              nested: { keep: true },
              values: [1, null, { kept: "yes" }],
            },
          },
        ],
        ["tenant.resolve_lens.get", undefined],
      ]);
      expect(savedView).toEqual(expectedSavedView);
      expect(renamedView).toEqual(expectedSavedView);
      expect(replacedView).toEqual(expectedSavedView);
      expect(savedLens).toEqual({
        id: 5,
        name: "Similar rows",
        sheetId: 3,
        spec: lensRow.spec,
        opId: 12,
        createdAt: "2026-08-10 00:02:00",
        updatedAt: "2026-08-10 00:03:00",
      });
      expect(resolved).toMatchObject({
        lensId: 5,
        rowIds: [7, 8],
        rows: [
          { rowId: 7, distance: 0.25, score: 0.75 },
          { rowId: 8, distance: null, score: null },
        ],
      });
    });
  });

  it("fails closed when generic Saved View JSON lacks an object filter", async () => {
    await withMockedTransport((operationId) => {
      if (operationId === "tenant.list_views.get") {
        return [{ ...viewRow, spec: { filter: null } }];
      }
      return transportResponse(operationId);
    }, async () => {
      const fresh = (await import("../../src/api/viewsLenses")) as ViewsLensesModule;
      const api = fresh.createViewsLensesDomainApi(
        (status, payload) => new MappedContractError(status, payload),
        "invalid-view",
      );

      await expect(api.listViews("3")).rejects.toThrow(
        "Saved View filter must be an object",
      );
    });
  });

  it("keeps the structured lens error envelope with ApiError.details undefined", async () => {
    // The lens 400 {detail:{code,message,field}} maps to status + message +
    // code, while `details` stays UNDEFINED. Folding `field` (or anything
    // else) into details must fail this contract.
    const real = await import("../../src/api/real");
    const projectApi = real.createProjectApi("view-lens-error");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          {
            detail: {
              code: "invalid_lens_spec",
              message: "query spec must be an object",
              field: "query",
            },
          },
          400,
        ),
      ),
    );
    const error = await projectApi
      .resolveLens(5)
      .then(() => undefined)
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(real.ApiError);
    const apiError = error as InstanceType<typeof real.ApiError>;
    expect(apiError.status).toBe(400);
    expect(apiError.message).toBe("query spec must be an object");
    expect(apiError.code).toBe("invalid_lens_spec");
    expect(
      apiError.details,
      "the lens envelope must not start populating ApiError.details",
    ).toBeUndefined();
    expect("details" in apiError && apiError.details !== undefined).toBe(false);
  });

  it("calls each generated operation with its own id", async () => {
    const module = await viewsLensesModule();
    expect(
      module,
      "INTENDED_F5_RED: viewsLenses generated adapter must exist",
    ).toBeDefined();
    if (!module) return;

    await withMockedTransport(transportResponse, async (calls) => {
      const fresh = (await import("../../src/api/viewsLenses").catch(
        () => undefined,
      )) as ViewsLensesModule | undefined;
      expect(fresh).toBeDefined();
      if (!fresh) return;
      const api = fresh.createViewsLensesApi(
        (status, payload) => new MappedContractError(status, payload),
        "view-lens-ids",
      ) as unknown as Record<string, (...args: unknown[]) => unknown>;

      for (const [method, operationId] of Object.entries(methodOperationIds)) {
        calls.length = 0;
        await api[method](1, {}, 0, {});
        expect(
          calls.map((call) => call.operationId),
          `${method} must call exactly its own generated operation`,
        ).toEqual([operationId]);
      }
    });
  });

  it("routes every realApi view/lens method through the generated transport", async () => {
    // THE LIVE-PATH PROOF: the object the product actually uses. The
    // transport is mocked BEFORE real.ts is imported, and everything is
    // pulled from that same fresh registry.
    await withMockedTransport(transportResponse, async (calls, fetchSpy) => {
      const real = await import("../../src/api/real");
      const projectApi = real.createProjectApi("view-lens-live");

      // Capture every outcome independently so a mapper failure cannot
      // short-circuit the transport coverage or hide later method calls.
      const results: Record<string, unknown> = {};
      const invoke = async (name: string, run: () => Promise<unknown>) => {
        try {
          results[name] = await run();
        } catch (error) {
          results[name] = error;
        }
      };
      await invoke("listViews", () => projectApi.listViews("3"));
      await invoke("saveView", () =>
        projectApi.saveView({
          name: "V",
          sheetId: "3",
          filter: { q: "x" },
          sort: null,
          columns: null,
          column_groups: null,
        }),
      );
      await invoke("renameView", () => projectApi.renameView(4, { name: "V2" }));
      await invoke("replaceViewDefinition", () => projectApi.replaceViewDefinition(4, {
        filter: { q: "updated" },
        sort: null,
        columns: null,
        column_groups: null,
      }));
      await invoke("deleteView", () => projectApi.deleteView(4));
      await invoke("listLenses", () => projectApi.listLenses(3));
      await invoke("saveLens", () =>
        projectApi.saveLens({ name: "L", query: { kind: "filter" } }),
      );
      await invoke("resolveLens", () =>
        projectApi.resolveLens(5, { limit: 5000, offset: 12 }),
      );

      expect(
        calls.map((call) => call.operationId),
        "every realApi view/lens method must ride its generated operation",
      ).toEqual(Object.values(realApiOperationIds));
      expect(
        fetchSpy,
        "no realApi view/lens method may bypass the generated transport with a direct fetch",
      ).not.toHaveBeenCalled();

      // Distinct domain mappings, preserved from Base.
      expect(results.listViews).toEqual([expectedSavedView]);
      expect(results.saveView).toEqual(expectedSavedView);
      expect(results.renameView).toEqual(expectedSavedView);
      expect(results.replaceViewDefinition).toEqual(expectedSavedView);
      const expectedLens = {
        id: 5,
        name: "Similar rows",
        sheetId: 3,
        spec: lensRow.spec,
        opId: 12,
        createdAt: "2026-08-10 00:02:00",
        updatedAt: "2026-08-10 00:03:00",
      };
      expect(results.listLenses).toEqual([expectedLens]);
      expect(results.saveLens).toEqual(expectedLens);
      const resolved = results.resolveLens as {
        lensId: number;
        rowIds: number[];
        rows: unknown[];
      };
      expect(resolved.lensId).toBe(5);
      expect(resolved.rowIds).toEqual([7, 8]);
      expect(resolved.rows).toEqual([
        { rowId: 7, distance: 0.25, score: 0.75 },
        { rowId: 8, distance: null, score: null },
      ]);
      // Deletes stay void at the port.
      expect(results.deleteView).toBeUndefined();
    });
  });

});
