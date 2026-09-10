import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  LocalProviderCatalog,
  LocalProviderEntry,
  LocalEndpointDiscoveryResponse,
  ModelPullDto,
  ModelPullStartResult,
  ProviderValidateResult,
} from "../../src/api/types";
import type {
  LocalEndpointInput,
  LocalEndpointPatch,
} from "../../src/api/localProviders";

const endpoint = {
  endpoint_id: "local-a1b2c3d4e5f6",
  label: "Desk Ollama",
  kind: "local_http",
  read_only: false,
  models: [
    {
      id: "ollama/@local-a1b2c3d4e5f6/smollm:135m",
      label: "smollm:135m",
      price: null,
      local: true,
    },
  ],
  reachable: true,
  origin: "http://127.0.0.1:11434",
  authority: "instance",
  source: "stored",
  detail: null,
  installed_models: ["smollm:135m"],
  protocol: "ollama_native",
  auth_status: "ok",
  token_configured: true,
  provisioning_token_configured: true,
  edge_auth: true,
  pull_enabled: true,
} as const;

const catalog = {
  schemaVersion: "frisket.providers.v1",
  tier: "local",
  network: "off",
  providers: [endpoint],
} as const;

const pull = {
  schemaVersion: "frisket.model_pull.v3",
  id: 7,
  model: "ollama/@local-a1b2c3d4e5f6/smollm:135m",
  status: "running",
  phase: null,
  total_bytes: null,
  completed_bytes: null,
  error: null,
  resolved_digest: null,
  resolved_size: null,
  created_at: "2026-08-09T12:00:00+00:00",
  started_at: null,
  finished_at: null,
  cancel_requested: false,
  endpoint_id: "local-a1b2c3d4e5f6",
  endpoint_origin: "http://127.0.0.1:11434",
  initiated_by: null,
  artifact: null,
} as const;

const validation = {
  provider: "openai",
  ok: true,
  reachable: true,
  status: 200,
  detail: null,
  validation_token: "validation-token",
};

interface Options {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

interface LocalProvidersApi {
  listProviders(projectId?: string, options?: Options): Promise<LocalProviderCatalog>;
  providerStatus(provider: string, options?: Options): Promise<LocalProviderEntry>;
  setProviderKey(provider: string, key: string, token?: string | null, options?: Options): Promise<LocalProviderCatalog>;
  deleteProviderKey(provider: string, options?: Options): Promise<LocalProviderCatalog>;
  createLocalEndpoint(input: LocalEndpointInput, options?: Options): Promise<LocalProviderCatalog>;
  discoverLocalEndpoints(options?: Options): Promise<LocalEndpointDiscoveryResponse>;
  updateLocalEndpoint(endpointId: string, patch: LocalEndpointPatch, options?: Options): Promise<LocalProviderCatalog>;
  deleteLocalEndpoint(endpointId: string, options?: Options): Promise<LocalProviderCatalog>;
  listModelPulls(options?: Options): Promise<{ pulls: ModelPullDto[] }>;
  getModelPull(id: number, options?: Options): Promise<ModelPullDto>;
  cancelModelPull(id: number, options?: Options): Promise<ModelPullDto>;
  startArtifactPull(ref: string, acknowledged?: boolean, options?: Options): Promise<ModelPullStartResult>;
  uninstallArtifact(ref: string, options?: Options): Promise<ModelPullDto>;
  validateProviderKey(provider: string, key?: string, options?: Options): Promise<ProviderValidateResult>;
}

class MappedContractError extends Error {
  constructor(readonly status: number, readonly payload: unknown) {
    super(`mapped contract error ${status}`);
    this.name = "MappedContractError";
  }
}

const errorFactory = (status: number, payload: unknown): Error =>
  new MappedContractError(status, payload);

async function localProvidersApi(): Promise<LocalProvidersApi> {
  const module = await import("../../src/api/localProviders");
  return module.createLocalProvidersApi(errorFactory);
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("local provider generated HTTP port", () => {
  it("owns the canonical endpoint CRUD and provider/pull transports", async () => {
    const responses = [
      catalog,
      endpoint,
      catalog,
      catalog,
      catalog,
      {
        candidates: [
          { label: "Ollama", origin: "http://localhost:11434", outcome: "added" },
        ],
      },
      catalog,
      catalog,
      { pulls: [pull] },
      pull,
      pull,
      { pull, deduplicated: true },
      pull,
      validation,
    ];
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      const status = requests.length === 11 || requests.length === 12 ? 202 : 200;
      return jsonResponse(responses.shift(), status);
    }));
    const controller = new AbortController();
    const options = {
      signal: controller.signal,
      headers: { Authorization: "Bearer local", "X-Trace-Id": "trace-local-providers" },
    };
    const api = await localProvidersApi();

    await api.listProviders("project/one", options);
    await api.providerStatus("local-a1b2c3d4e5f6", options);
    await api.setProviderKey("open/ai", "secret", "validation-token", options);
    await api.deleteProviderKey("open/ai", options);
    await api.createLocalEndpoint({
      display_name: "LM Studio",
      origin: "http://127.0.0.1:1234",
      inference_token: "inference-secret",
      provisioning_token: "pull-secret",
      edge_auth: true,
      pull_enabled: true,
    }, options);
    await api.discoverLocalEndpoints(options);
    await api.updateLocalEndpoint("local-a1b2c3d4e5f6", {
      display_name: "LM Studio PC",
      pull_enabled: false,
    }, options);
    await api.deleteLocalEndpoint("local-a1b2c3d4e5f6", options);
    await api.listModelPulls(options);
    await api.getModelPull(7, options);
    await api.cancelModelPull(7, options);
    await api.startArtifactPull("ollama/@local-a1b2c3d4e5f6/smollm:135m", true, options);
    await api.uninstallArtifact("opus-mt:en-es", options);
    await api.validateProviderKey("open/ai", "secret", options);

    expect(requests.map(({ input, init }) => [input, init?.method])).toEqual([
      ["/api/providers?project_id=project%2Fone", "GET"],
      ["/api/providers/local-a1b2c3d4e5f6/status", "GET"],
      ["/api/providers/keys/open%2Fai", "PUT"],
      ["/api/providers/keys/open%2Fai", "DELETE"],
      ["/api/providers/local-endpoints", "POST"],
      ["/api/providers/local-endpoints/discover", "POST"],
      ["/api/providers/local-endpoints/local-a1b2c3d4e5f6", "PATCH"],
      ["/api/providers/local-endpoints/local-a1b2c3d4e5f6", "DELETE"],
      ["/api/providers/models/pulls", "GET"],
      ["/api/providers/models/pulls/7", "GET"],
      ["/api/providers/models/pulls/7/cancel", "POST"],
      ["/api/providers/models/pull", "POST"],
      ["/api/providers/models/uninstall", "POST"],
      ["/api/providers/open%2Fai/validate", "POST"],
    ]);
    expect(requests.map(({ init }) => init?.body ?? null)).toEqual([
      null,
      null,
      JSON.stringify({ key: "secret", validation_token: "validation-token" }),
      null,
      JSON.stringify({
        display_name: "LM Studio",
        origin: "http://127.0.0.1:1234",
        inference_token: "inference-secret",
        provisioning_token: "pull-secret",
        edge_auth: true,
        pull_enabled: true,
      }),
      null,
      JSON.stringify({ display_name: "LM Studio PC", pull_enabled: false }),
      null,
      null,
      null,
      null,
      JSON.stringify({
        ref: "ollama/@local-a1b2c3d4e5f6/smollm:135m",
        unpinned_acknowledged: true,
      }),
      JSON.stringify({ ref: "opus-mt:en-es" }),
      JSON.stringify({ key: "secret" }),
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(controller.signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get("authorization")).toBe("Bearer local");
      expect(headers.get("x-trace-id")).toBe("trace-local-providers");
    }
  });

  it("preserves coded error detail and maps an additional non-2xx status", async () => {
    const responses = [
      jsonResponse({ detail: { code: "pull_busy", message: "busy", active: pull } }, 409),
      jsonResponse({ detail: "teapot" }, 418),
    ];
    vi.stubGlobal("fetch", vi.fn(async () => responses.shift()!));
    const api = await localProvidersApi();
    const ref = "ollama/@local-a1b2c3d4e5f6/smollm:135m";

    await expect(api.startArtifactPull(ref)).rejects.toMatchObject({
      name: "MappedContractError",
      status: 409,
      payload: { detail: { code: "pull_busy", active: pull } },
    });
    await expect(api.startArtifactPull(ref)).rejects.toMatchObject({
      name: "MappedContractError",
      status: 418,
      payload: { detail: "teapot" },
    });
  });
});
