import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("apache-arrow", () => ({ tableFromIPC: vi.fn() }));
vi.mock("pluralize", () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import type {
  WatchInfo,
  WatchInput,
  WatchRunEventsPage,
  WatchRunResult,
  WatchRunsPage,
} from "../../src/api/types";

const runRow = {
  id: 4,
  watch_id: 1,
  status: "ok",
  op_cursor_before: 0,
  op_cursor_after: 3,
  matched_rows: 2,
  new_rows: 1,
  error: null,
  error_code: null,
  resolved_query_hash: "sha256:abc",
  resolved_query: { kind: "search.fts", q: "budget" },
  started_at: "2026-08-10 00:00:00",
  finished_at: "2026-08-10 00:00:01",
};

const hitRow = {
  run_id: 4,
  sheet_id: 3,
  row_id: 7,
  column_id: null,
  rank: 1,
  snippet: "budget line",
  is_new: true,
};

const watchRow = {
  id: 1,
  name: "Budget",
  scope: "project",
  sheet_id: null,
  query: { kind: "fts", q: "budget", mode: "keyword", rerank: "off", limit: 50 },
  query_version: "frisket.query.v1",
  query_hash: "sha256:abc",
  detection_policy: { kind: "new_matches" },
  enabled: true,
  last_evaluated_op: 3,
  last_run_id: 4,
  last_status: "ok",
  created_at: "2026-08-10 00:00:00",
  updated_at: "2026-08-10 00:00:01",
  latest_run: runRow,
};

const runResultFixture = {
  schema_version: "frisket.watch_run.v1",
  watch: watchRow,
  run: runRow,
  hits: [hitRow],
};

const runsPageFixture = {
  schema_version: "frisket.watch_runs_page.v1",
  order: "desc",
  offset: 0,
  limit: 20,
  total: 1,
  has_more: false,
  next_offset: null,
  hits_limit: 20,
  runs: [{ ...runRow, hits: [hitRow], hits_limit: 20, hits_truncated: false }],
};

const eventRow = {
  id: 9,
  run_id: 4,
  watch_id: 1,
  event_kind: "row_entered",
  subject_kind: "row",
  subject_ref: { sheet_id: 3, row_id: 7 },
  before_json: null,
  after_json: { matched: true },
  delta_json: null,
  severity: "info",
  rank: 1,
  snippet: "budget line",
  created_at: "2026-08-10 00:00:01",
};

const eventsPageFixture = {
  schema_version: "frisket.watch_run_events_page.v1",
  order: "asc",
  offset: 0,
  limit: 50,
  total: 1,
  has_more: false,
  next_offset: null,
  events: [eventRow],
};

// The preserved Base domain shapes (toWatchRun / toWatchInfo / …).
const expectedRun = {
  id: 4,
  watchId: 1,
  status: "ok",
  opCursorBefore: 0,
  opCursorAfter: 3,
  matchedRows: 2,
  newRows: 1,
  error: null,
  errorCode: null,
  resolvedQueryHash: "sha256:abc",
  resolvedQuery: { kind: "search.fts", q: "budget" },
  startedAt: "2026-08-10T00:00:00Z",
  finishedAt: "2026-08-10T00:00:01Z",
};

const expectedHit = {
  runId: 4,
  sheetId: 3,
  rowId: 7,
  columnId: null,
  rank: 1,
  snippet: "budget line",
  isNew: true,
};

const expectedWatch = {
  id: 1,
  name: "Budget",
  scope: "project",
  sheetId: null,
  query: { kind: "fts", q: "budget", mode: "keyword", rerank: "off", limit: 50 },
  queryVersion: "frisket.query.v1",
  queryHash: "sha256:abc",
  enabled: true,
  lastEvaluatedOp: 3,
  lastRunId: 4,
  lastStatus: "ok",
  createdAt: "2026-08-10T00:00:00Z",
  updatedAt: "2026-08-10T00:00:01Z",
  latestRun: expectedRun,
};

const expectedEvent = {
  id: 9,
  runId: 4,
  watchId: 1,
  eventKind: "row_entered",
  subjectKind: "row",
  subjectRef: { sheet_id: 3, row_id: 7 },
  beforeJson: null,
  afterJson: { matched: true },
  deltaJson: null,
  severity: "info",
  rank: 1,
  snippet: "budget line",
  createdAt: "2026-08-10T00:00:01Z",
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

interface WatchesTransport {
  listWatches(options?: Options): Promise<unknown>;
  createWatch(body: Record<string, unknown>, options?: Options): Promise<unknown>;
  updateWatch(
    watchId: number,
    body: WatchPatchInput,
    options?: Options,
  ): Promise<unknown>;
  deleteWatch(watchId: number, options?: Options): Promise<unknown>;
  runWatch(watchId: number, options?: Options): Promise<unknown>;
  listWatchRuns(
    watchId: number,
    offset?: number | null,
    limit?: number | null,
    hitsLimit?: number | null,
    options?: Options,
  ): Promise<unknown>;
  listWatchRunEvents(
    watchId: number,
    runId: number,
    offset?: number | null,
    limit?: number | null,
    eventKind?: string | null,
    options?: Options,
  ): Promise<unknown>;
}

interface WatchesModule {
  createWatchesApi(
    factory: (status: number, payload: unknown) => Error,
    projectId: string,
  ): WatchesTransport;
  createWatchesDomainApi(
    factory: (status: number, payload: unknown) => Error,
    projectId: string,
  ): WatchesDomainTransport;
}

interface WatchesDomainTransport {
  listWatches(): Promise<WatchInfo[]>;
  createWatch(input: WatchInput): Promise<WatchInfo>;
  updateWatch(watchId: number, input: WatchPatchInput): Promise<WatchInfo>;
  deleteWatch(watchId: number): Promise<void>;
  runWatch(watchId: number): Promise<WatchRunResult>;
  getWatchRuns(
    watchId: number,
    offset?: number,
    limit?: number,
  ): Promise<WatchRunsPage>;
  getWatchRunEvents(
    watchId: number,
    runId: number,
    offset?: number,
    limit?: number,
    eventKind?: string,
  ): Promise<WatchRunEventsPage>;
}

/** The lifecycle PATCH is deliberately narrow: Watch query/binding state is
 * immutable after creation, so it cannot be smuggled through a rename/pause. */
interface WatchPatchInput {
  name?: string;
  enabled?: boolean;
}

interface WatchesLifecycleProjectApi {
  updateWatch(watchId: number, input: WatchPatchInput): Promise<WatchInfo>;
  deleteWatch(watchId: number): Promise<void>;
}

async function watchesModule(): Promise<WatchesModule | undefined> {
  const path = "../../src/api/watches";
  return import(/* @vite-ignore */ path).catch(() => undefined) as Promise<
    WatchesModule | undefined
  >;
}

class MappedContractError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`mapped watch error ${status}`);
    this.name = "MappedContractError";
    this.status = status;
    this.payload = payload;
  }
}

/** The seven browser operations, by adapter method. */
const methodOperationIds: Record<string, string> = {
  listWatches: "tenant.list_watches.get",
  createWatch: "tenant.create_watch.post",
  updateWatch: "tenant.patch_watch.patch",
  deleteWatch: "tenant.delete_watch.delete",
  runWatch: "tenant.run_watch.post",
  listWatchRuns: "tenant.list_watch_runs.get",
  listWatchRunEvents: "tenant.list_watch_run_events.get",
};

/** The realApi methods that must ride the generated transport, and the
 *  operation each one is expected to invoke. */
const realApiOperationIds: Record<string, string> = {
  listWatches: "tenant.list_watches.get",
  createWatch: "tenant.create_watch.post",
  updateWatch: "tenant.patch_watch.patch",
  deleteWatch: "tenant.delete_watch.delete",
  runWatch: "tenant.run_watch.post",
  getWatchRuns: "tenant.list_watch_runs.get",
  getWatchRunEvents: "tenant.list_watch_run_events.get",
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
  if (operationId === "tenant.list_watches.get") return [watchRow];
  if (operationId === "tenant.create_watch.post") return watchRow;
  if (operationId === "tenant.patch_watch.patch") {
    return { ...watchRow, name: "Renamed", enabled: false };
  }
  if (operationId === "tenant.delete_watch.delete") {
    return { ok: true, deleted: watchRow.id };
  }
  if (operationId === "tenant.run_watch.post") return runResultFixture;
  if (operationId === "tenant.list_watch_runs.get") return runsPageFixture;
  return eventsPageFixture;
}

afterEach(() => vi.unstubAllGlobals());

describe("watchlist generated HTTP transport", () => {
  it("owns exactly the seven operations and preserves path, query, body, and options", async () => {
    const module = await watchesModule();
    expect(
      module,
      "INTENDED_F6_RED: watches generated adapter must exist",
    ).toBeDefined();
    if (!module) return;
    const api = module.createWatchesApi(
      (status, payload) => new MappedContractError(status, payload),
      "watch-contract",
    );
    expect(Object.keys(api).sort()).toEqual([
      "createWatch",
      "deleteWatch",
      "listWatchRunEvents",
      "listWatchRuns",
      "listWatches",
      "runWatch",
      "updateWatch",
    ]);

    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses: unknown[] = [
      [watchRow],
      watchRow,
      { ...watchRow, name: "Renamed" },
      { ok: true, deleted: 1 },
      runResultFixture,
      runsPageFixture,
      runsPageFixture,
      eventsPageFixture,
      eventsPageFixture,
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
      headers: { Authorization: "Bearer watches", "X-Trace-Id": "f6" },
    };

    await api.listWatches(options);
    await api.createWatch(
      { name: "Budget", query: { kind: "fts", q: "budget" } },
      options,
    );
    await api.updateWatch(1, { name: "Renamed" }, options);
    await api.deleteWatch(1, options);
    await api.runWatch(1, options);
    await api.listWatchRuns(1, undefined, undefined, undefined, options);
    await api.listWatchRuns(1, 2, 3, 4, options);
    await api.listWatchRunEvents(1, 4, undefined, undefined, undefined, options);
    await api.listWatchRunEvents(1, 4, 1, 2, "row_entered", options);

    expect(
      requests.map((request) => [request.input, request.init?.method]),
    ).toEqual([
      ["/api/projects/watch-contract/watches", "GET"],
      ["/api/projects/watch-contract/watches", "POST"],
      ["/api/projects/watch-contract/watches/1", "PATCH"],
      ["/api/projects/watch-contract/watches/1", "DELETE"],
      ["/api/projects/watch-contract/watches/1/run", "POST"],
      ["/api/projects/watch-contract/watches/1/runs", "GET"],
      [
        "/api/projects/watch-contract/watches/1/runs?offset=2&limit=3&hits_limit=4",
        "GET",
      ],
      ["/api/projects/watch-contract/watches/1/runs/4/events", "GET"],
      [
        "/api/projects/watch-contract/watches/1/runs/4/events" +
          "?offset=1&limit=2&event_kind=row_entered",
        "GET",
      ],
    ]);
    // Lifecycle PATCH carries only the supplied mutable field. The Watch's
    // query/binding state is immutable after creation, and DELETE/run stay
    // bodyless.
    expect(requests.map((request) => request.init?.body)).toEqual([
      undefined,
      JSON.stringify({
        name: "Budget",
        query: { kind: "fts", q: "budget" },
      }),
      JSON.stringify({ name: "Renamed" }),
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get("authorization")).toBe("Bearer watches");
      expect(headers.get("x-trace-id")).toBe("f6");
    }
  });

  it("keeps the preserved realApi domain mapping for watches, runs, and events", async () => {
    const real = await import("../../src/api/real");
    const projectApi = real.createProjectApi("watch-domain");
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses: unknown[] = [
      [watchRow],
      watchRow,
      runResultFixture,
      runsPageFixture,
      eventsPageFixture,
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        requests.push({ input, init });
        return jsonResponse(responses.shift());
      }),
    );
    const listed = await projectApi.listWatches();
    const created = await projectApi.createWatch({
      name: "Budget",
      query: { kind: "fts", q: "budget" },
    });
    const runResult = await projectApi.runWatch(1);
    const runsPage = await projectApi.getWatchRuns(1, 2, 3);
    const eventsPage = await projectApi.getWatchRunEvents(1, 4, 1, 2, "row_entered");

    // WatchInfo keeps its camelCase fields and derived latestRun.
    expect(listed).toEqual([expectedWatch]);
    expect(created).toEqual(expectedWatch);
    // WatchRunResult keeps its literal schemaVersion and mapped hits.
    expect(runResult).toEqual({
      schemaVersion: "frisket.watch_run.v1",
      watch: expectedWatch,
      run: expectedRun,
      hits: [expectedHit],
    });
    // WatchRunsPage keeps camelCase paging; toWatchRun drops the per-run
    // hits_limit/hits_truncated wire keys and keeps mapped hits.
    expect(runsPage).toEqual({
      schemaVersion: "frisket.watch_runs_page.v1",
      order: "desc",
      offset: 0,
      limit: 20,
      total: 1,
      hasMore: false,
      nextOffset: null,
      runs: [{ ...expectedRun, hits: [expectedHit] }],
    });
    expect(eventsPage).toEqual({
      schemaVersion: "frisket.watch_run_events_page.v1",
      order: "asc",
      offset: 0,
      limit: 50,
      total: 1,
      hasMore: false,
      nextOffset: null,
      events: [expectedEvent],
    });

    expect(requests.map((request) => request.input)).toEqual([
      "/api/projects/watch-domain/watches",
      "/api/projects/watch-domain/watches",
      "/api/projects/watch-domain/watches/1/run",
      "/api/projects/watch-domain/watches/1/runs?offset=2&limit=3",
      "/api/projects/watch-domain/watches/1/runs/4/events" +
        "?offset=1&limit=2&event_kind=row_entered",
    ]);
    // Direct Watches send only their independently captured query.
    expect(requests[1].init?.body).toBe(
      JSON.stringify({
        name: "Budget",
        query: { kind: "fts", q: "budget" },
      }),
    );
  });

  it("keeps the plain-string watch error envelope on ApiError", async () => {
    // Watch errors are plain string details ({"detail": "watch not found"}):
    // status + message, with code and details both undefined.
    const real = await import("../../src/api/real");
    const projectApi = real.createProjectApi("watch-error");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "watch not found" }, 404)),
    );
    const error = await projectApi
      .runWatch(9999)
      .then(() => undefined)
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(real.ApiError);
    const apiError = error as InstanceType<typeof real.ApiError>;
    expect(apiError.status).toBe(404);
    expect(apiError.message).toBe("watch not found");
    expect(apiError.code).toBeUndefined();
    expect(apiError.details).toBeUndefined();
  });

  it("keeps typed lifecycle failures on the public project API", async () => {
    const real = await import("../../src/api/real");
    const lifecycleApi = real.createProjectApi(
      "watch-lifecycle-error",
    ) as unknown as WatchesLifecycleProjectApi;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "watch not found" }, 404)),
    );

    const [patchError, deleteError] = await Promise.all([
      lifecycleApi.updateWatch(9999, { enabled: false }).catch((caught: unknown) => caught),
      lifecycleApi.deleteWatch(9999).catch((caught: unknown) => caught),
    ]);

    for (const error of [patchError, deleteError]) {
      expect(error).toBeInstanceOf(real.ApiError);
      const apiError = error as InstanceType<typeof real.ApiError>;
      expect(apiError.status).toBe(404);
      expect(apiError.message).toBe("watch not found");
      expect(apiError.code).toBeUndefined();
      expect(apiError.details).toBeUndefined();
    }
  });

  it("calls each generated operation with its own id", async () => {
    const module = await watchesModule();
    expect(
      module,
      "INTENDED_F6_RED: watches generated adapter must exist",
    ).toBeDefined();
    if (!module) return;

    await withMockedTransport(transportResponse, async (calls) => {
      const fresh = (await import("../../src/api/watches").catch(
        () => undefined,
      )) as WatchesModule | undefined;
      expect(fresh).toBeDefined();
      if (!fresh) return;
      const api = fresh.createWatchesApi(
        (status, payload) => new MappedContractError(status, payload),
        "watch-ids",
      ) as unknown as Record<string, (...args: unknown[]) => unknown>;

      for (const [method, operationId] of Object.entries(methodOperationIds)) {
        calls.length = 0;
        await api[method](1, 4, 0, 20, undefined, {});
        expect(
          calls.map((call) => call.operationId),
          `${method} must call exactly its own generated operation`,
        ).toEqual([operationId]);
      }
    });
  });

  it(
    "maps narrow lifecycle PATCHes to complete Watches and DELETE acknowledgements to void",
    async () => {
    // Data flow: public/domain input -> generated request -> complete Watch
    // response. The lifecycle seam is intentionally narrow: neither PATCH
    // variant can carry a query or a binding edit.
    await withMockedTransport(transportResponse, async (calls) => {
      const fresh = (await import("../../src/api/watches")) as WatchesModule;
      const domain = fresh.createWatchesDomainApi(
        (status, payload) => new MappedContractError(status, payload),
        "watch-lifecycle",
      );

      const renamed = await domain.updateWatch(1, { name: "Renamed" });
      const paused = await domain.updateWatch(1, { enabled: false });
      const deleted = await domain.deleteWatch(1);

      expect(calls.map((call) => call.operationId)).toEqual([
        "tenant.patch_watch.patch",
        "tenant.patch_watch.patch",
        "tenant.delete_watch.delete",
      ]);
      expect(calls.map((call) => call.options.pathParams)).toEqual([
        { pid: "watch-lifecycle", watch_id: 1 },
        { pid: "watch-lifecycle", watch_id: 1 },
        { pid: "watch-lifecycle", watch_id: 1 },
      ]);
      expect(calls.map((call) => call.options.query)).toEqual([{}, {}, {}]);
      expect(calls[0].options.body).toEqual({ name: "Renamed" });
      expect(calls[1].options.body).toEqual({ enabled: false });
      expect(calls[0].options.body).not.toHaveProperty("query");
      expect(calls[1].options.body).not.toHaveProperty("query");
      expect(calls[0].options.body).not.toHaveProperty("binding_mode");
      expect(calls[1].options.body).not.toHaveProperty("binding_mode");
      expect(calls[2].options).not.toHaveProperty("body");

      // PATCH returns a whole Watch row, not a lifecycle-specific subset.
      expect(renamed).toEqual({
        ...expectedWatch,
        name: "Renamed",
        enabled: false,
      });
      expect(paused).toEqual({
        ...expectedWatch,
        name: "Renamed",
        enabled: false,
      });
      expect(deleted).toBeUndefined();
    });
  });

  it("routes every realApi watch method through the generated transport", async () => {
    // THE LIVE-PATH PROOF: the object the product actually uses. The
    // transport is mocked BEFORE real.ts is imported, and everything is
    // pulled from that same fresh registry.
    await withMockedTransport(transportResponse, async (calls, fetchSpy) => {
      const real = await import("../../src/api/real");
      const projectApi = real.createProjectApi("watch-live");

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
      await invoke("listWatches", () => projectApi.listWatches());
      await invoke("createWatch", () =>
        projectApi.createWatch({
          name: "Budget",
          query: { kind: "fts", q: "budget" },
        }),
      );
      const lifecycleApi = projectApi as unknown as WatchesLifecycleProjectApi;
      await invoke("updateWatch", () => lifecycleApi.updateWatch(1, { enabled: false }));
      await invoke("deleteWatch", () => lifecycleApi.deleteWatch(1));
      await invoke("runWatch", () => projectApi.runWatch(1));
      await invoke("getWatchRuns", () => projectApi.getWatchRuns(1, 2, 3));
      await invoke("getWatchRunEvents", () =>
        projectApi.getWatchRunEvents(1, 4, 1, 2, "row_entered"),
      );

      expect(
        calls.map((call) => call.operationId),
        "every realApi watch method must ride its generated operation",
      ).toEqual(Object.values(realApiOperationIds));
      expect(
        fetchSpy,
        "no realApi watch method may bypass the generated transport with a direct fetch",
      ).not.toHaveBeenCalled();

      // Distinct domain mappings, preserved from Base.
      expect(results.listWatches).toEqual([expectedWatch]);
      expect(results.createWatch).toEqual(expectedWatch);
      expect(results.updateWatch).toEqual({
        ...expectedWatch,
        name: "Renamed",
        enabled: false,
      });
      expect(results.deleteWatch).toBeUndefined();
      expect(results.runWatch).toEqual({
        schemaVersion: "frisket.watch_run.v1",
        watch: expectedWatch,
        run: expectedRun,
        hits: [expectedHit],
      });
      const runsPage = results.getWatchRuns as { runs: unknown[] };
      expect(runsPage.runs).toEqual([{ ...expectedRun, hits: [expectedHit] }]);
      const eventsPage = results.getWatchRunEvents as { events: unknown[] };
      expect(eventsPage.events).toEqual([expectedEvent]);
    });
  });

  it("owns synchronous domain normalization and captures its explicit project", async () => {
    const eventWithIdentity = {
      ...eventRow,
      before_json: { matched: false },
      after_json: { matched: true },
      delta_json: { matched: "changed" },
    };
    await withMockedTransport((operationId) => {
      if (operationId === "tenant.list_watch_runs.get") {
        return {
          ...runsPageFixture,
          runs: [
            { ...runRow, hits: [] },
            { ...runRow },
          ],
        };
      }
      if (operationId === "tenant.list_watch_run_events.get") {
        return { ...eventsPageFixture, events: [eventWithIdentity] };
      }
      if (operationId === "tenant.create_watch.post") {
        return {
          ...watchRow,
          query: { kind: "filter", sheet_id: 3, filter: { status: { eq: "open" } } },
        };
      }
      return transportResponse(operationId);
    }, async (calls) => {
      const fresh = (await import("../../src/api/watches")) as WatchesModule;
      const domain = fresh.createWatchesDomainApi(
        (status, payload) => new MappedContractError(status, payload),
        "watch-domain-a",
      );
      const input: WatchInput = {
        name: "Budget",
        query: { kind: "filter", sheet_id: 3, filter: { status: { eq: "open" } } },
        scope: { kind: "sheet", sheet_id: 3 },
        enabled: false,
      };
      const created = domain.createWatch(input);

      const [listed, createdWatch, runResult, runsPage, eventsPage] = await Promise.all([
        domain.listWatches(),
        created,
        domain.runWatch(1),
        domain.getWatchRuns(1),
        domain.getWatchRunEvents(1, 4, undefined, undefined, ""),
      ]);

      expect(calls.map((call) => call.operationId)).toEqual([
        "tenant.create_watch.post",
        "tenant.list_watches.get",
        "tenant.run_watch.post",
        "tenant.list_watch_runs.get",
        "tenant.list_watch_run_events.get",
      ]);
      expect(calls.map((call) => call.options.pathParams)).toEqual([
        { pid: "watch-domain-a" },
        { pid: "watch-domain-a" },
        { pid: "watch-domain-a", watch_id: 1 },
        { pid: "watch-domain-a", watch_id: 1 },
        { pid: "watch-domain-a", watch_id: 1, run_id: 4 },
      ]);
      expect(calls[0].options.body).toEqual({
        name: "Budget",
        query: { kind: "filter", sheet_id: 3, filter: { status: { eq: "open" } } },
        scope: { kind: "sheet", sheet_id: 3 },
        enabled: false,
      });
      expect(calls[0].options.body).not.toHaveProperty("detection_policy");
      expect(calls[0].options.body).not.toHaveProperty("source_view");
      expect(calls[0].options.body).not.toHaveProperty("source_view_id");
      expect(calls[1].options.query).toEqual({});
      expect(calls[2].options.query).toEqual({});
      expect(calls[2].options).not.toHaveProperty("body");
      expect(calls[3].options.query).toEqual({
        offset: 0,
        limit: 20,
        hits_limit: undefined,
      });
      expect(calls[4].options.query).toEqual({
        offset: 0,
        limit: 50,
        event_kind: undefined,
      });

      // Mapping allocates feature containers but leaves typed JSON subtrees
      // referentially intact, just as the legacy adapter did.
      expect(listed[0].query).toBe(watchRow.query);
      expect(createdWatch.query).toEqual({ kind: "filter", sheet_id: 3, filter: { status: { eq: "open" } } });
      expect(runResult.hits).not.toBe(runResultFixture.hits);
      expect(runResult.run.resolvedQuery).toBe(runRow.resolved_query);
      expect(runsPage.runs[0].hits).toEqual([]);
      expect(runsPage.runs[1]).not.toHaveProperty("hits");
      expect(eventsPage.events).not.toBe(eventsPageFixture.events);
      expect(eventsPage.events[0].subjectRef).toBe(eventWithIdentity.subject_ref);
      expect(eventsPage.events[0].beforeJson).toBe(eventWithIdentity.before_json);
      expect(eventsPage.events[0].afterJson).toBe(eventWithIdentity.after_json);
      expect(eventsPage.events[0].deltaJson).toBe(eventWithIdentity.delta_json);
    });
  });

  it("serializes an independent filter Watch without a Saved View source", async () => {
    await withMockedTransport(transportResponse, async (calls) => {
      const fresh = (await import("../../src/api/watches")) as WatchesModule;
      const domain = fresh.createWatchesDomainApi(
        (status, payload) => new MappedContractError(status, payload),
        "watch-independent-filter",
      );

      await domain.createWatch({
        name: "Open rows",
        query: { kind: "filter", sheet_id: 7, filter: { status: { eq: "open" } } },
        scope: { kind: "sheet", sheet_id: 7 },
      });

      expect(calls[0]).toMatchObject({
        operationId: "tenant.create_watch.post",
        options: {
          body: {
            name: "Open rows",
            query: { kind: "filter", sheet_id: 7, filter: { status: { eq: "open" } } },
            scope: { kind: "sheet", sheet_id: 7 },
          },
        },
      });
    });
  });

});
