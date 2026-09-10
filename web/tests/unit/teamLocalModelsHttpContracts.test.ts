import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("apache-arrow", () => ({ tableFromIPC: vi.fn() }));
vi.mock("pluralize", () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import {
  ApiError,
  listOrgLocalEndpoints,
  orgCancelModelPull,
  orgGetModelPull,
  orgListModelPulls,
  orgStartArtifactPull,
} from "../../src/api/real";
import { httpContract } from "../../src/api/httpContract";

const pull = {
  schemaVersion: "frisket.model_pull.v3",
  id: 7,
  model: "ollama/@local-a1b2c3d4e5f6/smollm:135m",
  status: "cancelled",
  phase: null,
  total_bytes: null,
  completed_bytes: null,
  error: null,
  resolved_digest: null,
  resolved_size: null,
  created_at: "2026-08-10T12:00:00+00:00",
  started_at: null,
  finished_at: "2026-08-10T12:01:00+00:00",
  cancel_requested: true,
  endpoint_id: "local-a1b2c3d4e5f6",
  endpoint_origin: "http://127.0.0.1:11434",
  initiated_by: "1",
  artifact: null,
};

const catalog = {
  schemaVersion: "frisket.local_endpoints.v1",
  endpoints: [{
    endpoint_id: "local-a1b2c3d4e5f6",
    label: "Organization Ollama",
    kind: "local_http",
    read_only: true,
    models: [{
      id: "ollama/@local-a1b2c3d4e5f6/smollm:135m",
      label: "smollm:135m",
      price: null,
      local: true,
    }],
    reachable: true,
    origin: "http://127.0.0.1:11434",
    authority: "organization",
    source: "environment",
    detail: null,
    installed_models: ["smollm:135m"],
    protocol: "ollama_native",
    auth_status: "ok",
    token_configured: false,
    provisioning_token_configured: false,
    edge_auth: false,
    pull_enabled: true,
  }],
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("team-local model generated HTTP transport", () => {
  it("keeps all five stable real exports on generated method/path/body transport", async () => {
    const responses = [
      catalog,
      { pull, deduplicated: false },
      { pulls: [pull] },
      pull,
      pull,
    ];
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> =
      [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        requests.push({ input, init });
        const status =
          requests.length === 2 || requests.length === 5 ? 202 : 200;
        return jsonResponse(responses.shift(), status);
      }),
    );

    await listOrgLocalEndpoints();
    await orgStartArtifactPull("hf:owner/model@sha/model.gguf", true);
    await orgListModelPulls();
    await orgGetModelPull(7);
    await expect(orgCancelModelPull(7)).resolves.toEqual(pull);

    expect(
      requests.map((request) => [request.input, request.init?.method]),
    ).toEqual([
      ["/api/org/local-endpoints", "GET"],
      ["/api/org/models/pull", "POST"],
      ["/api/org/models/pulls", "GET"],
      ["/api/org/models/pulls/7", "GET"],
      ["/api/org/models/pulls/7/cancel", "POST"],
    ]);
    expect(requests.map((request) => request.init?.body ?? null)).toEqual([
      null,
      JSON.stringify({
        ref: "hf:owner/model@sha/model.gguf",
        unpinned_acknowledged: true,
      }),
      null,
      null,
      null,
    ]);
  });

  it("uses the existing generated harness to encode a discriminating string path parameter", async () => {
    const requests: Array<RequestInfo | URL> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        requests.push(input);
        return jsonResponse({ deleted: true });
      }),
    );

    await httpContract(
      "outer.delete_org_key.delete",
      {
        pathParams: { provider: "open/ai?prod" },
        query: {},
      },
      (wire) => wire,
    );
    expect(requests).toEqual(["/api/org/keys/open%2Fai%3Fprod"]);
  });

  it("accepts the canonical empty plural catalog form the handler emits", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({
        schemaVersion: "frisket.local_endpoints.v1",
        endpoints: [],
      })),
    );
    await expect(listOrgLocalEndpoints()).resolves.toEqual({
      schemaVersion: "frisket.local_endpoints.v1",
      endpoints: [],
    });
  });

  it("maps an additional non-2xx status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "teapot" }, 418)),
    );
    await expect(
      orgStartArtifactPull("ollama/@local-a1b2c3d4e5f6/smollm:135m"),
    ).rejects.toMatchObject({
      name: ApiError.name,
      status: 418,
      message: "teapot",
    });
  });

  it("preserves a declared, fully typed pull_busy.active as ApiError details", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          { detail: { code: "pull_busy", message: "busy", active: pull } },
          409,
        ),
      ),
    );
    await expect(
      orgStartArtifactPull("ollama/@local-a1b2c3d4e5f6/smollm:135m"),
    ).rejects.toMatchObject({
      name: ApiError.name,
      status: 409,
      code: "pull_busy",
      details: { active: pull },
    });
  });

});
