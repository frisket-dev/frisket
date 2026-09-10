import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  OrgKeyInfo,
  ProviderCatalog,
  ProviderValidateResult,
} from "../../src/api/types";

interface Options {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

interface OrganizationProvidersApi {
  providerCatalog(options?: Options): Promise<ProviderCatalog>;
  listOrgKeys(options?: Options): Promise<OrgKeyInfo[]>;
  setOrgKey(
    provider: string,
    key: string,
    validationToken?: string | null,
    options?: Options,
  ): Promise<void>;
  validateOrgKey(
    provider: string,
    key?: string,
    options?: Options,
  ): Promise<ProviderValidateResult>;
  deleteOrgKey(provider: string, options?: Options): Promise<void>;
}

class MappedContractError extends Error {
  constructor(
    readonly status: number,
    readonly payload: unknown,
  ) {
    super(`mapped contract error ${status}`);
    this.name = "MappedContractError";
  }
}

async function organizationProvidersApi(): Promise<OrganizationProvidersApi> {
  const module = await import("../../src/api/organizationProviders");
  return module.createOrganizationProvidersApi(
    (status: number, payload: unknown) =>
      new MappedContractError(status, payload),
  );
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("organization provider generated HTTP port", () => {
  it("owns the five organization-provider calls while commands discard save/delete bodies", async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> =
      [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        requests.push({ input, init });
        return jsonResponse(
          [
            {
              schemaVersion: "frisket.provider_catalog.v1",
              providers: [],
            },
            [{ provider: "openai", hint: "…abcd" }],
            { provider: "openai", hint: "…abcd" },
            {
              provider: "openai",
              ok: true,
              reachable: true,
              status: 200,
              detail: null,
              validation_token: "validation-token",
            },
            { deleted: true },
          ][requests.length - 1],
        );
      }),
    );
    const controller = new AbortController();
    const options = {
      signal: controller.signal,
      headers: {
        Authorization: "Bearer organization",
        "X-Trace-Id": "org-keys",
      },
    };
    const api = await organizationProvidersApi();

    await api.providerCatalog(options);
    await api.listOrgKeys(options);
    await expect(
      api.setOrgKey("open/ai", "secret", "validation-token", options),
    ).resolves.toBeUndefined();
    await api.validateOrgKey("open/ai", "secret", options);
    await expect(api.deleteOrgKey("open/ai", options)).resolves.toBeUndefined();

    expect(
      requests.map((request) => [request.input, request.init?.method]),
    ).toEqual([
      ["/api/org/provider-catalog", "GET"],
      ["/api/org/keys", "GET"],
      ["/api/org/keys", "POST"],
      ["/api/org/keys/validate", "POST"],
      ["/api/org/keys/open%2Fai", "DELETE"],
    ]);
    expect(requests.map((request) => request.init?.body ?? null)).toEqual([
      null,
      null,
      JSON.stringify({
        provider: "open/ai",
        key: "secret",
        validation_token: "validation-token",
      }),
      JSON.stringify({ provider: "open/ai", key: "secret" }),
      null,
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(controller.signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get("authorization")).toBe("Bearer organization");
      expect(headers.get("x-trace-id")).toBe("org-keys");
    }
  });

  it("maps declared and additional non-2xx statuses", async () => {
    const responses = [
      jsonResponse({ detail: "unsupported provider" }, 400),
      jsonResponse({ detail: "teapot" }, 418),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => responses.shift()!),
    );
    const api = await organizationProvidersApi();

    await expect(api.validateOrgKey("bogus")).rejects.toMatchObject({
      name: "MappedContractError",
      status: 400,
      payload: { detail: "unsupported provider" },
    });
    await expect(api.validateOrgKey("bogus")).rejects.toMatchObject({
      name: "MappedContractError",
      status: 418,
      payload: { detail: "teapot" },
    });
  });

  it("keeps real.ts stable while composing void save/delete commands", async () => {
    const real = await import("../../src/api/real");
    const responses = [
      jsonResponse({ provider: "openai", hint: "…abcd" }),
      jsonResponse({ deleted: true }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => responses.shift()!),
    );

    await expect(real.setOrgKey("openai", "secret")).resolves.toBeUndefined();
    await expect(real.deleteOrgKey("openai")).resolves.toBeUndefined();
  });

});
