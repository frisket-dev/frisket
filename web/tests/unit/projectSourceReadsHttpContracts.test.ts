import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("apache-arrow", () => ({ tableFromIPC: vi.fn() }));
vi.mock("pluralize", () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));


const rawRun = {
  id: 8,
  source_id: 7,
  op_id: null,
  receipt_id: "receipt-8",
  status: "ok",
  new_rows: 1,
  skipped_rows: 2,
  changed_rows: 3,
  revisions: 4,
  error: "detail-run-error",
  cursor_before: "raw-before",
  cursor_after: "raw-after",
  duration_ms: 5,
  warning_count: 6,
  cost_micro: 7,
  summary_json: '{"kept":"raw"}',
  started_at: "2026-08-10 00:00:00",
  finished_at: "2026-08-10 00:01:00",
};

const rawLatestRun = {
  ...rawRun,
  id: 18,
  receipt_id: "receipt-18",
  error: "detail-latest-run-error",
  started_at: "2026-08-10 00:04:00",
  finished_at: "2026-08-10 00:05:00",
};

function rawSource(config: unknown, id = 7) {
  return {
    id,
    name: `source-${id}`,
    kind: "rss",
    url: "https://example.test/feed",
    config,
    sheet_id: 3,
    schedule: "@hourly",
    enabled: true,
    cursor: "raw-source-cursor",
    last_checked_at: "2026-08-10 00:00:00",
    last_status: "ok",
    new_rows_total: 1,
    created_at: "2026-08-09 23:00:00",
  };
}

const detailFixture = {
  ...rawSource({ nested: ["value"] }),
  runs: [rawRun],
  runs_page: {
    schema_version: "frisket.source_runs_page.v1",
    order: "desc",
    offset: 2,
    limit: 3,
    total: 5,
    has_more: true,
    next_offset: 5,
    latest_run: rawLatestRun,
    latest_run_loaded: true,
  },
};

const healthFixture = {
  schema_version: "frisket.source_health.v1",
  source: {
    id: 7,
    name: "source-7",
    kind: "rss",
    url: "https://example.test/feed",
    enabled: true,
    schedule: "@hourly",
    sheet_id: 3,
    created_at: "2026-08-08 04:05:06",
    redactions: ["config", "cursor"],
  },
  summary: {
    status: "healthy",
    last_success_at: "2026-08-10 00:00:00",
    last_failure_at: "2026-08-09 22:00:00",
    consecutive_failures: 0,
    new_rows_total: 1,
    new_rows_recent: 1,
    changed_rows_recent: 2,
    skipped_rows_recent: 3,
    revisions_recent: 4,
    recent_run_count: 1,
    last_cursor_summary: "stored",
  },
  runs_page: {
    schema_version: "frisket.source_runs_page.v1",
    order: "desc",
    offset: 4,
    limit: 5,
    total: 6,
    has_more: true,
    next_offset: 9,
  },
  runs: [
    {
      id: 8,
      source_id: 7,
      status: "ok",
      started_at: "2026-08-10 00:00:00",
      finished_at: "2026-08-10 00:01:00",
      receipt_id: "receipt-8",
      op_id: 11,
      new_rows: 1,
      skipped_rows: 2,
      changed_rows: 3,
      revisions: 4,
      duration_ms: 5,
      warning_count: 6,
      cost_micro: 7,
      error_summary: "health-run-error",
      cursor_before_present: true,
      cursor_after_present: true,
      summary_present: true,
    },
  ],
  downstream_jobs: [
    {
      job_id: 9,
      kind: "source.poll",
      status: "done",
      attempts: 1,
      max_attempts: 3,
      created_at: "2026-08-10T00:00:00Z",
      started_at: "2026-08-10T00:02:00Z",
      finished_at: "2026-08-10T00:03:00Z",
      refs: { source_id: 7 },
      result_summary: { kept: [1] },
      error_summary: "downstream-error",
      stalled: false,
    },
  ],
  costs: {
    recent_actual_micro: 7,
    recent_estimated_micro: 0,
    basis:
      "source_runs.cost_micro for loaded runs; downstream costs unknown-safe",
  },
  alerts: [
    {
      code: "unsupported_source_schedule",
      message: "Source schedule could not be parsed for stale detection.",
    },
  ],
  warnings: [
    {
      code: "sparse_source_run_metadata",
      message:
        "Some loaded source runs lack v1 runtime metadata; row deltas and costs may be incomplete.",
      run_ids: [8],
    },
  ],
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

interface ProjectSourcesTransport {
  listSources(options?: Options): Promise<Array<{ config: unknown }>>;
  getSource(
    id: number,
    offset?: number | null,
    limit?: number,
    options?: Options,
  ): Promise<{ runs: unknown[] }>;
  getSourceHealth(
    id: number,
    offset?: number | null,
    limit?: number,
    options?: Options,
  ): Promise<{ source: { redactions: string[] } }>;
  createSource(input: unknown): Promise<{ id: number; config: unknown }>;
  updateSource(id: number, patch: unknown): Promise<{ id: number }>;
  deleteSource(id: number): Promise<void>;
  fetchSource(id: number): Promise<{
    runId: number;
    newRows: number;
    revisions: number;
    skippedRows: number;
    changedRows: number;
    warningCount: number;
    sheetId: number | null;
  }>;
}

interface ProjectSourcesActionSession {
  registeredProjectActionSpec(
    actionId: string,
    params: Record<string, unknown>,
  ): Record<string, unknown>;
  v1ActionSpec(
    kind: string,
    capabilities: string[],
    params: Record<string, unknown>,
  ): Record<string, unknown>;
  withV1ActionResult<T>(
    spec: Record<string, unknown>,
    handler: (result: { outputs?: Array<{ kind: string; ref?: unknown }> }) => T | Promise<T>,
    options?: { clearIdempotency?: 'finally' },
  ): Promise<T>;
}

interface ProjectSourcesModule {
  createProjectSourcesApi(
    dependencies: {
      errorFactory: (status: number, payload: unknown) => Error;
      v1ActionSession: ProjectSourcesActionSession;
    },
    projectId: string,
  ): ProjectSourcesTransport;
}

async function projectSourcesModule(): Promise<
  ProjectSourcesModule | undefined
> {
  const path = "../../src/api/projectSources";
  return import(/* @vite-ignore */ path).catch(() => undefined) as Promise<
    ProjectSourcesModule | undefined
  >;
}

class MappedContractError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`mapped source error ${status}`);
    this.name = "MappedContractError";
    this.status = status;
    this.payload = payload;
  }
}

afterEach(() => vi.unstubAllGlobals());

describe("project-source generated HTTP reads", () => {
  it("owns only the three raw source reads and preserves path/query/options", async () => {
    const module = await projectSourcesModule();
    expect(
      module,
      "INTENDED_F4_RED: projectSources generated adapter must exist",
    ).toBeDefined();
    if (!module) return;
    const actionSession: ProjectSourcesActionSession = {
      registeredProjectActionSpec: () => {
        throw new Error('source reads must not create registered actions');
      },
      v1ActionSpec: () => {
        throw new Error('source reads must not create v1 actions');
      },
      withV1ActionResult: () => {
        throw new Error('source reads must not post v1 actions');
      },
    };
    const api = module.createProjectSourcesApi({
      errorFactory: (status, payload) => new MappedContractError(status, payload),
      v1ActionSession: actionSession,
    }, "source-read-contract");
    expect(Object.keys(api).sort()).toEqual([
      "createSource",
      "deleteSource",
      "fetchSource",
      "getSource",
      "getSourceHealth",
      "listSources",
      "updateSource",
    ]);

    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> =
      [];
    const responses = [
      [rawSource(null)],
      detailFixture,
      healthFixture,
      detailFixture,
      healthFixture,
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
      headers: { Authorization: "Bearer source-read", "X-Trace-Id": "f4" },
    };

    const rawList = await api.listSources(options);
    await api.getSource(7, null, 3, options);
    await api.getSourceHealth(8, 4, 5, options);
    await api.getSource(9, undefined, undefined, options);
    await api.getSourceHealth(10, undefined, undefined, options);

    expect(rawList[0].config).toEqual({});
    expect(
      requests.map((request) => [request.input, request.init?.method]),
    ).toEqual([
      ["/api/projects/source-read-contract/sources", "GET"],
      ["/api/projects/source-read-contract/sources/7?runs_limit=3", "GET"],
      [
        "/api/projects/source-read-contract/sources/8/health?runs_offset=4&runs_limit=5",
        "GET",
      ],
      ["/api/projects/source-read-contract/sources/9?runs_limit=50", "GET"],
      [
        "/api/projects/source-read-contract/sources/10/health?runs_limit=20",
        "GET",
      ],
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get("authorization")).toBe("Bearer source-read");
      expect(headers.get("x-trace-id")).toBe("f4");
    }
  });

  it("owns source mutations through the injected action session", async () => {
    const module = await projectSourcesModule();
    expect(module).toBeDefined();
    if (!module) return;

    const specs: Array<{
      kind: string;
      capabilities: string[];
      params: Record<string, unknown>;
    }> = [];
    const postOptions: Array<{ clearIdempotency?: 'finally' } | undefined> = [];
    const results = [
      {
        outputs: [{
          kind: "source",
          ref: {
            source_id: 7,
            name: "source-7",
            source_kind: "rss",
            url: "https://example.test/feed",
            config: { nested: ["value"] },
            sheet_id: 3,
            schedule: "@hourly",
            enabled: true,
            last_checked_at: "2026-08-10 00:00:00",
            last_status: "ok",
            new_rows_total: 1,
          },
        }],
      },
      {
        outputs: [{
          kind: "source",
          ref: {
            source_id: 7,
            name: "renamed-source",
            source_kind: "rss",
            new_rows_total: 2,
          },
        }],
      },
      { outputs: [] },
      {
        outputs: [{
          kind: "source_run",
          ref: {
            source_run_id: 8,
            new_rows: 1,
            revisions: 4,
            skipped_rows: 2,
            changed_rows: 3,
            warning_count: 6,
            sheet_id: 3,
          },
        }],
      },
    ];
    const actionSession: ProjectSourcesActionSession = {
      registeredProjectActionSpec(actionId, params) {
        const spec = { action_id: actionId, scope: { kind: "project" }, params, output_names: {} };
        specs.push(spec);
        return spec;
      },
      v1ActionSpec(kind, capabilities, params) {
        specs.push({ kind, capabilities, params });
        return { kind, capabilities, params };
      },
      async withV1ActionResult(spec, handler, options) {
        postOptions.push(options);
        const result = results.shift();
        expect(result, `missing result for ${String(spec.action_id ?? spec.kind)}`).toBeDefined();
        return handler(result ?? {});
      },
    };
    const api = module.createProjectSourcesApi({
      errorFactory: (status, payload) => new MappedContractError(status, payload),
      v1ActionSession: actionSession,
    }, "source-action-contract");

    await expect(api.createSource({
      name: "source-7",
      kind: "rss",
      url: "https://example.test/feed",
      schedule: "@hourly",
      enabled: true,
      config: { nested: ["value"] },
    })).resolves.toMatchObject({ id: 7, config: { nested: ["value"] } });
    await expect(api.updateSource(7, {
      name: "renamed-source",
      url: null,
      enabled: false,
    })).resolves.toMatchObject({ id: 7, name: "renamed-source" });
    await expect(api.deleteSource(7)).resolves.toBeUndefined();
    await expect(api.fetchSource(7)).resolves.toEqual({
      runId: 8,
      newRows: 1,
      revisions: 4,
      skippedRows: 2,
      changedRows: 3,
      warningCount: 6,
      sheetId: 3,
    });

    expect(specs).toEqual([
      {
        action_id: "source.create",
        scope: { kind: "project" },
        output_names: {},
        params: {
          name: "source-7",
          kind: "rss",
          url: "https://example.test/feed",
          schedule: "@hourly",
          enabled: true,
          config: { nested: ["value"] },
        },
      },
      {
        action_id: "source.update",
        scope: { kind: "project" },
        output_names: {},
        params: {
          source_id: 7,
          patch: {
            name: "renamed-source",
            url: null,
            enabled: false,
          },
        },
      },
      {
        action_id: "source.delete",
        scope: { kind: "project" },
        output_names: {},
        params: { source_id: 7 },
      },
      {
        action_id: "source.poll",
        scope: { kind: "project" },
        output_names: {},
        params: { source: 7 },
      },
    ]);
    expect(postOptions).toEqual([
      { clearIdempotency: "finally" },
      { clearIdempotency: "finally" },
      { clearIdempotency: "finally" },
      { clearIdempotency: "finally" },
    ]);
    await expect(api.fetchSource(0)).rejects.toMatchObject({ status: 400 });
  });

  it("posts source CRUD through the registered project action contract", async () => {
    const real = await import("../../src/api/real");
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      bodies.push(body);
      const actionId = String(body.action_id);
      const sourceRef = {
        source_id: 7,
        name: actionId === "source.update" ? "renamed-source" : "source-7",
        source_kind: "rss",
        url: null,
        config: {},
        sheet_id: null,
        schedule: null,
        enabled: true,
        last_checked_at: null,
        last_status: null,
        new_rows_total: 0,
      };
      return jsonResponse({
        schema_version: "frisket.action_result.v1",
        action: { kind: actionId, action_id: `action-${actionId}` },
        status: "completed",
        project_id: "registered-source-project",
        run_id: null,
        receipt_id: null,
        outputs: actionId === "source.delete"
          ? []
          : [{ kind: "source", name: "source", ref: sourceRef }],
        errors: [],
      });
    }));
    const api = real.createProjectApi("registered-source-project");

    await api.createSource({ name: "source-7", kind: "rss" });
    await api.updateSource(7, { name: "renamed-source" });
    await api.deleteSource(7);

    expect(bodies).toHaveLength(3);
    expect(bodies).toMatchObject([
      {
        action_id: "source.create",
        scope: { kind: "project" },
        params: { name: "source-7", kind: "rss" },
        output_names: {},
      },
      {
        action_id: "source.update",
        scope: { kind: "project" },
        params: { source_id: 7, patch: { name: "renamed-source" } },
        output_names: {},
      },
      {
        action_id: "source.delete",
        scope: { kind: "project" },
        params: { source_id: 7 },
        output_names: {},
      },
    ]);
    for (const body of bodies) {
      expect(body).not.toHaveProperty("kind");
      expect(body).not.toHaveProperty("capabilities");
      expect(body.idempotency_key).toMatch(/^web-source\.(create|update|delete):/);
    }
  });

  it("keeps stable realApi domain mapping and config compatibility", async () => {
    const real = await import("../../src/api/real");
    const configs = [{ nested: ["value"] }, [], "", 0, false, null];
    const responses: unknown[] = [
      [rawSource(configs[0])],
      detailFixture,
      healthFixture,
      ...configs
        .slice(1)
        .map((config, index) => [rawSource(config, index + 20)]),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(responses.shift())),
    );
    const projectApi = real.createProjectApi("source-domain-mapping");
    const listed = await projectApi.listSources();
    const detail = await projectApi.getSource(7, 2, 3);
    const health = await projectApi.getSourceHealth(7, 4, 5);
    const mappedConfigs = [listed[0].config];
    for (let index = 1; index < configs.length; index += 1) {
      mappedConfigs.push((await projectApi.listSources())[0].config);
    }

    expect(mappedConfigs).toEqual([
      { nested: ["value"] },
      [],
      "",
      0,
      false,
      {},
    ]);
    const expectedSourceInfo = {
      id: 7,
      name: "source-7",
      kind: "rss",
      url: "https://example.test/feed",
      config: { nested: ["value"] },
      sheetId: 3,
      schedule: "@hourly",
      enabled: true,
      lastCheckedAt: "2026-08-10T00:00:00Z",
      lastStatus: "ok",
      newRowsTotal: 1,
    };
    const expectedRun = {
      id: 8,
      status: "ok",
      newRows: 1,
      skippedRows: 2,
      changedRows: 3,
      revisions: 4,
      warningCount: 6,
      durationMs: 5,
      costMicro: 7,
      receiptId: "receipt-8",
      error: "detail-run-error",
      startedAt: "2026-08-10T00:00:00Z",
      finishedAt: "2026-08-10T00:01:00Z",
    };
    const expectedLatestRun = {
      id: 18,
      status: "ok",
      newRows: 1,
      skippedRows: 2,
      changedRows: 3,
      revisions: 4,
      warningCount: 6,
      durationMs: 5,
      costMicro: 7,
      receiptId: "receipt-18",
      error: "detail-latest-run-error",
      startedAt: "2026-08-10T00:04:00Z",
      finishedAt: "2026-08-10T00:05:00Z",
    };

    expect(listed).toEqual([expectedSourceInfo]);
    expect(detail).toEqual({
      ...expectedSourceInfo,
      runs: [expectedRun],
      runsPage: {
        schemaVersion: "frisket.source_runs_page.v1",
        order: "desc",
        offset: 2,
        limit: 3,
        total: 5,
        hasMore: true,
        nextOffset: 5,
        latestRun: expectedLatestRun,
        latestRunLoaded: true,
      },
    });
    expect(detail).not.toHaveProperty("cursor");
    expect(detail).not.toHaveProperty("createdAt");
    expect(detail.runs[0]).not.toHaveProperty("sourceId");
    expect(detail.runs[0]).not.toHaveProperty("opId");
    expect(detail.runs[0]).not.toHaveProperty("cursorBefore");
    expect(detail.runs[0]).not.toHaveProperty("cursorAfter");
    expect(detail.runs[0]).not.toHaveProperty("summaryJson");

    expect(health).toEqual({
      schemaVersion: "frisket.source_health.v1",
      source: {
        id: 7,
        name: "source-7",
        kind: "rss",
        url: "https://example.test/feed",
        enabled: true,
        schedule: "@hourly",
        sheetId: 3,
        createdAt: "2026-08-08T04:05:06Z",
        redactions: ["config", "cursor"],
      },
      summary: {
        status: "healthy",
        lastSuccessAt: "2026-08-10T00:00:00Z",
        lastFailureAt: "2026-08-09T22:00:00Z",
        consecutiveFailures: 0,
        newRowsTotal: 1,
        newRowsRecent: 1,
        changedRowsRecent: 2,
        skippedRowsRecent: 3,
        revisionsRecent: 4,
        recentRunCount: 1,
        lastCursorSummary: "stored",
      },
      runsPage: {
        schemaVersion: "frisket.source_runs_page.v1",
        order: "desc",
        offset: 4,
        limit: 5,
        total: 6,
        hasMore: true,
        nextOffset: 9,
      },
      runs: [
        {
          id: 8,
          status: "ok",
          newRows: 1,
          skippedRows: 2,
          changedRows: 3,
          revisions: 4,
          warningCount: 6,
          durationMs: 5,
          costMicro: 7,
          receiptId: "receipt-8",
          error: "health-run-error",
          startedAt: "2026-08-10T00:00:00Z",
          finishedAt: "2026-08-10T00:01:00Z",
          sourceId: 7,
          opId: 11,
          errorSummary: "health-run-error",
          cursorBeforePresent: true,
          cursorAfterPresent: true,
          summaryPresent: true,
        },
      ],
      downstreamJobs: [
        {
          jobId: 9,
          kind: "source.poll",
          status: "done",
          attempts: 1,
          maxAttempts: 3,
          createdAt: "2026-08-10T00:00:00Z",
          startedAt: "2026-08-10T00:02:00Z",
          finishedAt: "2026-08-10T00:03:00Z",
          refs: { source_id: 7 },
          resultSummary: { kept: [1] },
          errorSummary: "downstream-error",
          stalled: false,
        },
      ],
      costs: {
        recentActualMicro: 7,
        recentEstimatedMicro: 0,
        basis:
          "source_runs.cost_micro for loaded runs; downstream costs unknown-safe",
      },
      alerts: [
        {
          code: "unsupported_source_schedule",
          message: "Source schedule could not be parsed for stale detection.",
        },
      ],
      warnings: [
        {
          code: "sparse_source_run_metadata",
          message:
            "Some loaded source runs lack v1 runtime metadata; row deltas and costs may be incomplete.",
          run_ids: [8],
        },
      ],
    });
    expect(health.source).not.toHaveProperty("config");
    expect(health.source).not.toHaveProperty("cursor");
  });

});
