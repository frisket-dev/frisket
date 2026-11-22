import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("pluralize", () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { createNotificationsApi } from "../../src/api/notifications";
import { jsonResponse } from '../support/httpContractTestHelpers';

const itemRow = {
  id: 1,
  source_kind: "watch",
  source_ref: { watch_id: 5 },
  source_event_ids: [1, 2],
  event_count: 2,
  event_kinds: ["row_entered"],
  title: "Budget watch",
  summary: "2 new rows",
  severity: "warning",
  deep_link: { view: "watch", watch_id: 5 },
  created_at: "2026-08-11 00:00:00",
  updated_at: "2026-08-11 00:00:01",
  state: "unseen",
  seen_at: null,
  read_at: null,
  acknowledged_at: null,
  acknowledged_by: null,
};

const pageFixture = {
  schema_version: "frisket.notifications_page.v1",
  order: "desc",
  offset: 0,
  limit: 50,
  total: 1,
  has_more: false,
  next_offset: null,
  notifications: [itemRow],
};

const summaryFixture = {
  schema_version: "frisket.notifications_summary.v1",
  total: 2,
  unseen: 1,
  seen: 1,
  read: 0,
  acknowledged: 0,
  by_severity: { info: 1, warning: 1 },
  by_source_kind: { system: 1, watch: 1 },
  by_source_ref: [
    { source_kind: "watch", source_ref: { watch_id: 5 }, unseen: 1 },
  ],
};

const actorStateFixture = {
  notification_id: 1,
  actor_id: "local:project",
  state: "read",
  seen_at: "2026-08-11 00:00:02",
  read_at: "2026-08-11 00:00:02",
  acknowledged_at: null,
  acknowledged_by: null,
  updated_at: "2026-08-11 00:00:02",
};

// The preserved Base domain shapes (toNotificationItem / …, toIso applied).
const expectedItem = {
  id: 1,
  sourceKind: "watch",
  sourceRef: { watch_id: 5 },
  sourceEventIds: [1, 2],
  eventCount: 2,
  eventKinds: ["row_entered"],
  title: "Budget watch",
  summary: "2 new rows",
  severity: "warning",
  deepLink: { view: "watch", watch_id: 5 },
  createdAt: "2026-08-11T00:00:00Z",
  updatedAt: "2026-08-11T00:00:01Z",
  state: "unseen",
  seenAt: null,
  readAt: null,
  acknowledgedAt: null,
  acknowledgedBy: null,
};

const expectedSummary = {
  schemaVersion: "frisket.notifications_summary.v1",
  total: 2,
  unseen: 1,
  seen: 1,
  read: 0,
  acknowledged: 0,
  bySeverity: { info: 1, warning: 1 },
  bySourceKind: { system: 1, watch: 1 },
  bySourceRef: [
    { sourceKind: "watch", sourceRef: { watch_id: 5 }, unseen: 1 },
  ],
};

const expectedActorState = {
  notificationId: 1,
  actorId: "local:project",
  state: "read",
  seenAt: "2026-08-11T00:00:02Z",
  readAt: "2026-08-11T00:00:02Z",
  acknowledgedAt: null,
  acknowledgedBy: null,
  updatedAt: "2026-08-11T00:00:02Z",
};

/** The six notification read operations, each with a call that reaches it. */
const readInvocations: Array<
  [string, (api: ReturnType<typeof createNotificationsApi>) => Promise<unknown>]
> = [
  ["tenant.list_notifications.get", (api) => api.listNotifications()],
  ["tenant.notifications_summary.get", (api) => api.getNotificationsSummary()],
  [
    "tenant.mark_notifications_seen.post",
    (api) => api.markNotificationsSeen({ notificationIds: [1] }),
  ],
  ["tenant.mark_notification_read.post", (api) => api.markNotificationRead(1)],
  ["tenant.ack_notification.post", (api) => api.ackNotification(1)],
  ["tenant.unack_notification.post", (api) => api.unackNotification(1)],
];

/** The realApi methods that must ride the generated transport, and the
 *  operation each one is expected to invoke. */
const realApiOperationIds: Record<string, string> = {
  listNotifications: "tenant.list_notifications.get",
  getNotificationsSummary: "tenant.notifications_summary.get",
  markNotificationsSeen: "tenant.mark_notifications_seen.post",
  markNotificationRead: "tenant.mark_notification_read.post",
  ackNotification: "tenant.ack_notification.post",
  unackNotification: "tenant.unack_notification.post",
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
  if (operationId === "tenant.list_notifications.get") return pageFixture;
  if (operationId === "tenant.notifications_summary.get") return summaryFixture;
  if (operationId === "tenant.mark_notifications_seen.post")
    return { seen_count: 2 };
  return actorStateFixture;
}

afterEach(() => vi.unstubAllGlobals());

describe("notification reads generated HTTP transport", () => {
  it("owns the fourteen notification operations and preserves path, query, method, and body", async () => {
    const api = createNotificationsApi("notification-contract");
    expect(Object.keys(api).sort()).toEqual([
      "ackNotification",
      "createNotificationChannel",
      "createNotificationRoute",
      "getNotificationsSummary",
      "listNotificationChannels",
      "listNotificationDeliveryRequests",
      "listNotificationRoutes",
      "listNotifications",
      "markNotificationRead",
      "markNotificationsSeen",
      "testNotificationRoute",
      "unackNotification",
      "updateNotificationChannel",
      "updateNotificationRoute",
    ]);

    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses: unknown[] = [
      pageFixture,
      pageFixture,
      summaryFixture,
      { seen_count: 2 },
      actorStateFixture,
      actorStateFixture,
      actorStateFixture,
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        requests.push({ input, init });
        return jsonResponse(responses.shift());
      }),
    );

    await api.listNotifications();
    await api.listNotifications({
      state: "unseen",
      sourceKind: "watch",
      sourceRef: { watch_id: 5 },
      severity: "warning",
      offset: 2,
      limit: 3,
    });
    await api.getNotificationsSummary();
    await api.markNotificationsSeen({ notificationIds: [7] });
    await api.markNotificationRead(1);
    await api.ackNotification(1);
    await api.unackNotification(1);

    expect(
      requests.map((request) => [request.input, request.init?.method]),
    ).toEqual([
      [
        "/api/projects/notification-contract/notifications" +
          "?state=all&offset=0&limit=50",
        "GET",
      ],
      [
        "/api/projects/notification-contract/notifications" +
          "?state=unseen&offset=2&limit=3&source_kind=watch" +
          "&source_ref=%7B%22watch_id%22%3A5%7D&severity=warning",
        "GET",
      ],
      ["/api/projects/notification-contract/notifications/summary", "GET"],
      ["/api/projects/notification-contract/notifications/seen", "POST"],
      ["/api/projects/notification-contract/notifications/1/read", "POST"],
      ["/api/projects/notification-contract/notifications/1/ack", "POST"],
      ["/api/projects/notification-contract/notifications/1/unack", "POST"],
    ]);
    // The three per-item POSTs stay BODYLESS: no request payload rides the
    // wire. Bodies appear only on the seen-filter POST.
    expect(requests.map((request) => request.init?.body)).toEqual([
      undefined,
      undefined,
      undefined,
      JSON.stringify({ notification_ids: [7] }),
      undefined,
      undefined,
      undefined,
    ]);
  });

  it("keeps the preserved realApi domain mapping for pages, summary, and states", async () => {
    const real = await import("../../src/api/real");
    const projectApi = real.createProjectApi("notification-domain");
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses: unknown[] = [
      pageFixture,
      summaryFixture,
      { seen_count: 2 },
      actorStateFixture,
      actorStateFixture,
      actorStateFixture,
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        requests.push({ input, init });
        return jsonResponse(responses.shift());
      }),
    );
    const page = await projectApi.listNotifications({
      state: "unseen",
      sourceKind: "watch",
      sourceRef: { watch_id: 5 },
      severity: "warning",
      offset: 2,
      limit: 3,
    });
    const summary = await projectApi.getNotificationsSummary();
    const seen = await projectApi.markNotificationsSeen({
      notificationIds: [7],
    });
    const read = await projectApi.markNotificationRead(1);
    const itemAck = await projectApi.ackNotification(1);
    const itemUnack = await projectApi.unackNotification(1);

    // NotificationPage keeps its camelCase envelope and mapped items.
    expect(page).toEqual({
      schemaVersion: "frisket.notifications_page.v1",
      order: "desc",
      offset: 0,
      limit: 50,
      total: 1,
      hasMore: false,
      nextOffset: null,
      notifications: [expectedItem],
    });
    expect(summary).toEqual(expectedSummary);
    expect(seen).toEqual({ seenCount: 2 });
    expect(read).toEqual(expectedActorState);
    expect(itemAck).toEqual(expectedActorState);
    expect(itemUnack).toEqual(expectedActorState);

    expect(requests.map((request) => request.input)).toEqual([
      "/api/projects/notification-domain/notifications" +
        "?state=unseen&offset=2&limit=3&source_kind=watch" +
        "&source_ref=%7B%22watch_id%22%3A5%7D&severity=warning",
      "/api/projects/notification-domain/notifications/summary",
      "/api/projects/notification-domain/notifications/seen",
      "/api/projects/notification-domain/notifications/1/read",
      "/api/projects/notification-domain/notifications/1/ack",
      "/api/projects/notification-domain/notifications/1/unack",
    ]);
    // Filter POST bodies contain only supplied keys. The adapter-level matrix
    // above separately asserts that per-item POSTs stay bodyless.
    expect(requests[2].init?.body).toBe(
      JSON.stringify({ notification_ids: [7] }),
    );
  });

  it("calls each generated operation with its own id", async () => {
    await withMockedTransport(transportResponse, async (calls) => {
      const fresh = await import("../../src/api/notifications");
      const api = fresh.createNotificationsApi("notification-ids");

      for (const [operationId, invoke] of readInvocations) {
        calls.length = 0;
        await invoke(api);
        expect(
          calls.map((call) => call.operationId),
          `${operationId} must be called exactly once by its own method`,
        ).toEqual([operationId]);
      }
    });
  });

  it("routes every realApi notification method through the generated transport", async () => {
    // THE LIVE-PATH PROOF: the object the product actually uses. The
    // transport is mocked BEFORE real.ts is imported, and everything is
    // pulled from that same fresh registry.
    await withMockedTransport(transportResponse, async (calls, fetchSpy) => {
      const real = await import("../../src/api/real");
      const projectApi = real.createProjectApi("notification-live");

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
      await invoke("listNotifications", () =>
        projectApi.listNotifications({ state: "unseen" }),
      );
      await invoke("getNotificationsSummary", () =>
        projectApi.getNotificationsSummary(),
      );
      await invoke("markNotificationsSeen", () =>
        projectApi.markNotificationsSeen({ notificationIds: [7] }),
      );
      await invoke("markNotificationRead", () =>
        projectApi.markNotificationRead(1),
      );
      await invoke("ackNotification", () => projectApi.ackNotification(1));
      await invoke("unackNotification", () => projectApi.unackNotification(1));

      expect(
        calls.map((call) => call.operationId),
        "every realApi notification method must ride its generated operation",
      ).toEqual(Object.values(realApiOperationIds));
      expect(
        fetchSpy,
        "no realApi notification method may bypass the generated transport with a direct fetch",
      ).not.toHaveBeenCalled();

      // Distinct domain mappings, preserved from Base.
      const page = results.listNotifications as { notifications: unknown[] };
      expect(page.notifications).toEqual([expectedItem]);
      expect(results.getNotificationsSummary).toEqual(expectedSummary);
      expect(results.markNotificationsSeen).toEqual({ seenCount: 2 });
      expect(results.markNotificationRead).toEqual(expectedActorState);
      expect(results.ackNotification).toEqual(expectedActorState);
      expect(results.unackNotification).toEqual(expectedActorState);
    });
  });

});
