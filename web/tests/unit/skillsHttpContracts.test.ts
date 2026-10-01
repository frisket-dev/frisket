import { afterEach, describe, expect, it, vi } from "vitest";

import { skillsApi } from "../../src/api/skills";

const skill = {
  id: "skill/1",
  name: "document-investigation",
  description: "Read sources.",
  content: "# Read",
  enabled: true,
  revision: 7,
  createdAt: "2026-10-01T00:00:00Z",
  updatedAt: "2026-10-01T00:00:00Z",
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

describe("skills HTTP contracts", () => {
  it("uses generated contracts for list, create, update, enable, and delete", async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> =
      [];
    const responses = [
      jsonResponse({ schemaVersion: "frisket.skills.v1", skills: [skill] }),
      jsonResponse(skill),
      jsonResponse({ ...skill, revision: 8, content: "# Updated" }),
      jsonResponse({ ...skill, revision: 9, enabled: false }),
      new Response(null, { status: 204 }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        requests.push({ input, init });
        return responses.shift()!;
      }),
    );

    await expect(skillsApi.list()).resolves.toEqual({
      schemaVersion: "frisket.skills.v1",
      skills: [skill],
    });
    await expect(skillsApi.create("# Create")).resolves.toEqual(skill);
    await expect(skillsApi.update(skill, "# Updated")).resolves.toMatchObject({
      revision: 8,
    });
    await expect(skillsApi.setEnabled(skill, false)).resolves.toMatchObject({
      enabled: false,
    });
    await expect(skillsApi.delete(skill)).resolves.toBeUndefined();

    expect(requests.map(({ input }) => input)).toEqual([
      "/api/skills",
      "/api/skills",
      "/api/skills/skill%2F1",
      "/api/skills/skill%2F1/enabled",
      "/api/skills/skill%2F1",
    ]);
    expect(requests.map(({ init }) => init?.method)).toEqual([
      "GET",
      "POST",
      "PUT",
      "PATCH",
      "DELETE",
    ]);
    expect(requests.slice(1).map(({ init }) => init?.body)).toEqual([
      JSON.stringify({ content: "# Create", enabled: true }),
      JSON.stringify({ expectedRevision: 7, content: "# Updated" }),
      JSON.stringify({ expectedRevision: 7, enabled: false }),
      JSON.stringify({ expectedRevision: 7 }),
    ]);
    for (const { init } of requests.slice(1)) {
      expect(new Headers(init?.headers).get("content-type")).toBe(
        "application/json",
      );
    }
  });

  it("keeps the raw Markdown upload protocol because it has no declared JSON body", async () => {
    const fetch = vi.fn(async () => jsonResponse(skill));
    vi.stubGlobal("fetch", fetch);

    await expect(skillsApi.upload("# Uploaded")).resolves.toEqual(skill);

    expect(fetch).toHaveBeenCalledWith("/api/skills/upload", {
      method: "POST",
      headers: { "content-type": "text/markdown; charset=utf-8" },
      body: "# Uploaded",
    });
  });
});
