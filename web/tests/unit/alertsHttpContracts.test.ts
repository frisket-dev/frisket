import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("pluralize", () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import type { AlertInput } from "../../src/api/types";

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

const alertRow = {
  id: 1,
  name: "Budget",

  sheet_id: null,
  view_id: 4,
  schedule: '@hourly',
  detection_policy: { kind: "conditions", entered: true, exited: false, count_changed: false, fields: [] },
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
  watch: alertRow,
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

// The preserved Base domain shapes (toAlertCheck / toAlertInfo / …).
const expectedRun = {
  id: 4,
  alertId: 1,
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

const expectedAlert = {
  id: 1,
  name: "Budget",

  sheetId: null,
  viewId: 4,
  schedule: '@hourly',
  detectionPolicy: { kind: "conditions", entered: true, exited: false, count_changed: false, fields: [] },
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
  alertId: 1,
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

type AlertsModule = typeof import("../../src/api/alerts");

/** The seven browser operations, by public method. */
const alertOperationIds: Record<string, string> = {
  listAlerts: "tenant.list_watches.get",
  createAlert: "tenant.create_watch.post",
  updateAlert: "tenant.patch_watch.patch",
  deleteAlert: "tenant.delete_watch.delete",
  checkAlert: "tenant.run_watch.post",
  getAlertChecks: "tenant.list_watch_runs.get",
  getAlertCheckEvents: "tenant.list_watch_run_events.get",
};

interface TransportCall {
  operationId: string;
  options: { pathParams?: unknown; query?: unknown; body?: unknown };
}

/** Install a scoped mock of the generated transport, run `body` against a
 *  FRESH module registry, then restore. Everything the code under test needs
 *  must be imported INSIDE `body` so it comes from that same registry. The
 *  mock applies the caller's response mapper exactly as the real transport
 *  does. */
async function withMockedTransport<T>(
  respond: (operationId: string) => unknown,
  body: (calls: TransportCall[], fetchSpy: ReturnType<typeof vi.fn>) => Promise<T>,
): Promise<T> {
  const calls: TransportCall[] = [];
  vi.resetModules();
  vi.doMock("../../src/api/httpContract", () => ({
    httpContract: (
      operationId: string,
      options: TransportCall["options"],
      mapResponse?: (wire: unknown) => unknown,
    ) => {
      calls.push({ operationId, options });
      const wire = respond(operationId);
      return Promise.resolve(mapResponse ? mapResponse(wire) : wire);
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
  if (operationId === "tenant.list_watches.get") return [alertRow];
  if (operationId === "tenant.create_watch.post") return alertRow;
  if (operationId === "tenant.patch_watch.patch") {
    return { ...alertRow, name: "Renamed", enabled: false };
  }
  if (operationId === "tenant.delete_watch.delete") {
    return { ok: true, deleted: alertRow.id };
  }
  if (operationId === "tenant.run_watch.post") return runResultFixture;
  if (operationId === "tenant.list_watch_runs.get") return runsPageFixture;
  return eventsPageFixture;
}

afterEach(() => vi.unstubAllGlobals());

describe("alert generated HTTP transport", () => {
  it("owns exactly the seven operations and preserves path, query, and body", async () => {
    const module: AlertsModule = await import("../../src/api/alerts");
    const api = module.createAlertsApi("watch-contract");
    expect(Object.keys(api).sort()).toEqual([
      "checkAlert",
      "createAlert",
      "deleteAlert",
      "getAlertCheckEvents",
      "getAlertChecks",
      "listAlerts",
      "updateAlert",
    ]);

    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses: unknown[] = [
      [alertRow],
      alertRow,
      { ...alertRow, name: "Renamed" },
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

    await api.listAlerts();
    await api.createAlert({ name: "Budget", view_id: 4 });
    await api.updateAlert(1, { name: "Renamed" });
    await api.deleteAlert(1);
    await api.checkAlert(1);
    await api.getAlertChecks(1);
    await api.getAlertChecks(1, 2, 3);
    await api.getAlertCheckEvents(1, 4);
    await api.getAlertCheckEvents(1, 4, 1, 2, "row_entered");

    expect(
      requests.map((request) => [request.input, request.init?.method]),
    ).toEqual([
      ["/api/projects/watch-contract/watches", "GET"],
      ["/api/projects/watch-contract/watches", "POST"],
      ["/api/projects/watch-contract/watches/1", "PATCH"],
      ["/api/projects/watch-contract/watches/1", "DELETE"],
      ["/api/projects/watch-contract/watches/1/run", "POST"],
      ["/api/projects/watch-contract/watches/1/runs?offset=0&limit=20", "GET"],
      ["/api/projects/watch-contract/watches/1/runs?offset=2&limit=3", "GET"],
      [
        "/api/projects/watch-contract/watches/1/runs/4/events?offset=0&limit=50",
        "GET",
      ],
      [
        "/api/projects/watch-contract/watches/1/runs/4/events" +
          "?offset=1&limit=2&event_kind=row_entered",
        "GET",
      ],
    ]);
    // Lifecycle PATCH carries only the supplied mutable field. The Alert's
    // query/binding state is immutable after creation, and DELETE/run stay
    // bodyless.
    expect(requests.map((request) => request.init?.body)).toEqual([
      undefined,
      JSON.stringify({
        name: "Budget",
        view_id: 4,
      }),
      JSON.stringify({ name: "Renamed" }),
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
    ]);
    expect(
      requests.map((request) =>
        request.init?.headers === undefined
          ? undefined
          : [...new Headers(request.init.headers).entries()],
      ),
    ).toEqual([
      undefined,
      [["content-type", "application/json"]],
      [["content-type", "application/json"]],
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
    ]);
  });

  it("keeps the preserved realApi domain mapping for alerts, runs, and events", async () => {
    const real = await import("../../src/api/real");
    const projectApi = real.createProjectApi("watch-domain");
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses: unknown[] = [
      [alertRow],
      alertRow,
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
    const listed = await projectApi.listAlerts();
    const created = await projectApi.createAlert({
      name: "Budget",
      view_id: 4,
    });
    const runResult = await projectApi.checkAlert(1);
    const runsPage = await projectApi.getAlertChecks(1, 2, 3);
    const eventsPage = await projectApi.getAlertCheckEvents(1, 4, 1, 2, "row_entered");

    // AlertInfo keeps its camelCase fields and derived latestRun.
    expect(listed).toEqual([expectedAlert]);
    expect(created).toEqual(expectedAlert);
    // WatchRunResult keeps its literal schemaVersion and mapped hits.
    expect(runResult).toEqual({
      schemaVersion: "frisket.watch_run.v1",
      alert: expectedAlert,
      run: expectedRun,
      hits: [expectedHit],
    });
    // WatchRunsPage keeps camelCase paging; toAlertCheck drops the per-run
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
    // Direct Alerts send only their independently captured query.
    expect(requests[1].init?.body).toBe(
      JSON.stringify({
        name: "Budget",
        view_id: 4,
      }),
    );
  });

  it('preserves manual schedule null on create and patch instead of dropping it', async () => {
    const { createProjectApi } = await import('../../src/api/real');
    const projectApi = createProjectApi('manual-alert');
    const bodies: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_input, init) => {
      bodies.push(JSON.parse(init.body));
      return jsonResponse({ ...alertRow, schedule: null });
    }));
    expect((await projectApi.createAlert({ name: 'Manual', view_id: 4, schedule: null })).schedule).toBeNull();
    expect((await projectApi.updateAlert(1, { schedule: null })).schedule).toBeNull();
    expect(bodies).toEqual([{ name: 'Manual', view_id: 4, schedule: null }, { schedule: null }]);
  });

  it("keeps the plain-string alert error envelope on ApiError", async () => {
    // Alert errors are plain string details ({"detail": "alert not found"}):
    // status + message, with code and details both undefined.
    const real = await import("../../src/api/real");
    const projectApi = real.createProjectApi("watch-error");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "alert not found" }, 404)),
    );
    const error = await projectApi
      .checkAlert(9999)
      .then(() => undefined)
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(real.ApiError);
    const apiError = error as InstanceType<typeof real.ApiError>;
    expect(apiError.status).toBe(404);
    expect(apiError.message).toBe("alert not found");
    expect(apiError.code).toBeUndefined();
    expect(apiError.details).toBeUndefined();
  });

  it("keeps typed lifecycle failures on the public project API", async () => {
    const real = await import("../../src/api/real");
    const lifecycleApi = real.createProjectApi("watch-lifecycle-error");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "alert not found" }, 404)),
    );

    const [patchError, deleteError] = await Promise.all([
      lifecycleApi.updateAlert(9999, { enabled: false }).catch((caught: unknown) => caught),
      lifecycleApi.deleteAlert(9999).catch((caught: unknown) => caught),
    ]);

    for (const error of [patchError, deleteError]) {
      expect(error).toBeInstanceOf(real.ApiError);
      const apiError = error as InstanceType<typeof real.ApiError>;
      expect(apiError.status).toBe(404);
      expect(apiError.message).toBe("alert not found");
      expect(apiError.code).toBeUndefined();
      expect(apiError.details).toBeUndefined();
    }
  });

  it("calls each generated operation with its own id", async () => {
    await withMockedTransport(transportResponse, async (calls) => {
      const fresh: AlertsModule = await import("../../src/api/alerts");
      const api = fresh.createAlertsApi("watch-ids");
      const invokers: Record<string, () => Promise<unknown>> = {
        listAlerts: () => api.listAlerts(),
        createAlert: () => api.createAlert({ name: "Budget", view_id: 4 }),
        updateAlert: () => api.updateAlert(1, { enabled: false }),
        deleteAlert: () => api.deleteAlert(1),
        checkAlert: () => api.checkAlert(1),
        getAlertChecks: () => api.getAlertChecks(1),
        getAlertCheckEvents: () => api.getAlertCheckEvents(1, 4),
      };
      expect(Object.keys(invokers).sort()).toEqual(Object.keys(api).sort());

      for (const [method, operationId] of Object.entries(alertOperationIds)) {
        calls.length = 0;
        await invokers[method]();
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
    // Data flow: public/domain input -> generated request -> complete Alert
    // response. The lifecycle seam is intentionally narrow: neither PATCH
    // variant can carry a query or a binding edit.
    await withMockedTransport(transportResponse, async (calls) => {
      const fresh: AlertsModule = await import("../../src/api/alerts");
      const domain = fresh.createAlertsApi("watch-lifecycle");

      const renamed = await domain.updateAlert(1, { name: "Renamed" });
      const paused = await domain.updateAlert(1, { enabled: false });
      const deleted = await domain.deleteAlert(1);

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
      expect(calls.map((call) => call.options.query)).toEqual([
        undefined,
        undefined,
        undefined,
      ]);
      expect(calls[0].options.body).toEqual({ name: "Renamed" });
      expect(calls[1].options.body).toEqual({ enabled: false });
      expect(calls[0].options.body).not.toHaveProperty("query");
      expect(calls[1].options.body).not.toHaveProperty("query");
      expect(calls[0].options.body).not.toHaveProperty("binding_mode");
      expect(calls[1].options.body).not.toHaveProperty("binding_mode");
      expect(calls[2].options).not.toHaveProperty("body");

      // PATCH returns a whole Alert row, not a lifecycle-specific subset.
      expect(renamed).toEqual({
        ...expectedAlert,
        name: "Renamed",
        enabled: false,
      });
      expect(paused).toEqual({
        ...expectedAlert,
        name: "Renamed",
        enabled: false,
      });
      expect(deleted).toBeUndefined();
    });
  });

  it("routes every realApi alert method through the generated transport", async () => {
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
      await invoke("listAlerts", () => projectApi.listAlerts());
      await invoke("createAlert", () =>
        projectApi.createAlert({
          name: "Budget",
          view_id: 4,
        }),
      );
      await invoke("updateAlert", () => projectApi.updateAlert(1, { enabled: false }));
      await invoke("deleteAlert", () => projectApi.deleteAlert(1));
      await invoke("checkAlert", () => projectApi.checkAlert(1));
      await invoke("getAlertChecks", () => projectApi.getAlertChecks(1, 2, 3));
      await invoke("getAlertCheckEvents", () =>
        projectApi.getAlertCheckEvents(1, 4, 1, 2, "row_entered"),
      );

      expect(
        calls.map((call) => call.operationId),
        "every realApi watch method must ride its generated operation",
      ).toEqual(Object.values(alertOperationIds));
      expect(
        fetchSpy,
        "no realApi watch method may bypass the generated transport with a direct fetch",
      ).not.toHaveBeenCalled();

      // Distinct domain mappings, preserved from Base.
      expect(results.listAlerts).toEqual([expectedAlert]);
      expect(results.createAlert).toEqual(expectedAlert);
      expect(results.updateAlert).toEqual({
        ...expectedAlert,
        name: "Renamed",
        enabled: false,
      });
      expect(results.deleteAlert).toBeUndefined();
      expect(results.checkAlert).toEqual({
        schemaVersion: "frisket.watch_run.v1",
        alert: expectedAlert,
        run: expectedRun,
        hits: [expectedHit],
      });
      const runsPage = results.getAlertChecks as { runs: unknown[] };
      expect(runsPage.runs).toEqual([{ ...expectedRun, hits: [expectedHit] }]);
      const eventsPage = results.getAlertCheckEvents as { events: unknown[] };
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
          ...alertRow,
          view_id: 4,
        };
      }
      return transportResponse(operationId);
    }, async (calls) => {
      const fresh: AlertsModule = await import("../../src/api/alerts");
      const domain = fresh.createAlertsApi("watch-domain-a");
      const input: AlertInput = {
        name: "Budget",
        view_id: 4,

        enabled: false,
      };
      const created = domain.createAlert(input);

      const [listed, createdAlert, runResult, runsPage, eventsPage] = await Promise.all([
        domain.listAlerts(),
        created,
        domain.checkAlert(1),
        domain.getAlertChecks(1),
        domain.getAlertCheckEvents(1, 4, undefined, undefined, ""),
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
        view_id: 4,

        enabled: false,
      });
      expect(calls[0].options.body).not.toHaveProperty("detection_policy");
      expect(calls[0].options.body).not.toHaveProperty("source_view");
      expect(calls[0].options.body).not.toHaveProperty("source_view_id");
      expect(calls[1].options).not.toHaveProperty("query");
      expect(calls[2].options).not.toHaveProperty("query");
      expect(calls[2].options).not.toHaveProperty("body");
      expect(calls[3].options.query).toEqual({
        offset: 0,
        limit: 20,
      });
      expect(calls[4].options.query).toEqual({
        offset: 0,
        limit: 50,
        event_kind: undefined,
      });

      // Mapping allocates feature containers but leaves typed JSON subtrees
      // referentially intact, just as the legacy adapter did.
      expect(listed[0].detectionPolicy).toBe(alertRow.detection_policy);
      expect(createdAlert.viewId).toBe(4);
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

  it("serializes alert configuration by saved View identity without copying criteria", async () => {
    await withMockedTransport(transportResponse, async (calls) => {
      const fresh: AlertsModule = await import("../../src/api/alerts");
      const domain = fresh.createAlertsApi("watch-independent-filter");

      await domain.createAlert({
        name: "Open rows",
        view_id: 4,

      });

      expect(calls[0]).toMatchObject({
        operationId: "tenant.create_watch.post",
        options: {
          body: {
            name: "Open rows",
            view_id: 4,

          },
        },
      });
    });
  });

});
