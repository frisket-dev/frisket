import { afterEach, describe, expect, it, vi } from "vitest";

import type { createOrganizationProvidersApi } from "../../src/api/organizationProviders";
import { jsonResponse } from '../support/httpContractTestHelpers';

async function organizationProvidersApi(): Promise<
  ReturnType<typeof createOrganizationProvidersApi>
> {
  const module = await import("../../src/api/organizationProviders");
  return module.createOrganizationProvidersApi();
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
    const options = { signal: controller.signal };
    const api = await organizationProvidersApi();

    await api.providerCatalog(options);
    await api.listOrganizationKeys(options);
    await expect(
      api.setOrganizationKey("open/ai", "secret", "validation-token", options),
    ).resolves.toBeUndefined();
    await api.validateOrganizationKey("open/ai", "secret", options);
    await expect(api.deleteOrganizationKey("open/ai", options)).resolves.toBeUndefined();

    expect(
      requests.map((request) => [request.input, request.init?.method]),
    ).toEqual([
      ["/api/organization/provider-catalog", "GET"],
      ["/api/organization/keys", "GET"],
      ["/api/organization/keys", "POST"],
      ["/api/organization/keys/validate", "POST"],
      ["/api/organization/keys/open%2Fai", "DELETE"],
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
    }
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

    await expect(real.setOrganizationKey("openai", "secret")).resolves.toBeUndefined();
    await expect(real.deleteOrganizationKey("openai")).resolves.toBeUndefined();
  });

});
