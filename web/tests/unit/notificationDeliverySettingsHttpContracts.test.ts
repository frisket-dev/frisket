import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("apache-arrow", () => ({ tableFromIPC: vi.fn() }));
vi.mock("pluralize", () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { createProjectApi } from "../../src/api/real";

const realApi = createProjectApi('p');

const delivery = {
  id: 7,
  route_id: 1,
  channel_id: 2,
  notification_id: null,
  digest_run_id: null,
  delivery_kind: "test",
  dedupe_key: "notification:test:route:1:channel:2:fixture",
  status: "queued",
  job_id: null,
  available_at: "2026-08-11 00:00:00",
  last_error: null,
  provider_ref: null,
  created_at: "2026-08-11 00:00:00",
  updated_at: "2026-08-11 00:00:00",
  sent_at: null,
};

const channel = {
  id: 2,
  kind: "email",
  name: "Ops",
  enabled: true,
  owner_kind: "project",
  config: {},
  has_secret: false,
  created_at: "2026-08-11 00:00:00",
  updated_at: "2026-08-11 00:00:00",
};

const route = {
  id: 1,
  name: "Watch",
  enabled: true,
  owner_kind: "project",
  owner_ref: "p",
  recipient_actor_id: null,
  channel_id: 2,
  source_kind: null,
  source_ref_match: {},
  event_kinds: [],
  severity_min: "info",
  delivery_mode: "immediate",
  digest_cadence: null,
  digest_timezone: "UTC",
  digest_anchor_time: null,
  template_key: null,
  created_at: "2026-08-11 00:00:00",
  updated_at: "2026-08-11 00:00:00",
};

const domainChannel = {
  id: 2,
  kind: "email",
  name: "Ops",
  enabled: true,
  ownerKind: "project",
  config: {},
  hasSecret: false,
  createdAt: "2026-08-11T00:00:00Z",
  updatedAt: "2026-08-11T00:00:00Z",
};

const domainRoute = {
  id: 1,
  name: "Watch",
  enabled: true,
  ownerKind: "project",
  ownerRef: "p",
  recipientActorId: null,
  channelId: 2,
  sourceKind: null,
  sourceRefMatch: {},
  eventKinds: [],
  severityMin: "info",
  deliveryMode: "immediate",
  digestCadence: null,
  digestTimezone: "UTC",
  digestAnchorTime: null,
  templateKey: null,
  createdAt: "2026-08-11T00:00:00Z",
  updatedAt: "2026-08-11T00:00:00Z",
};

const domainDelivery = {
  id: 7,
  routeId: 1,
  channelId: 2,
  notificationId: null,
  digestRunId: null,
  deliveryKind: "test",
  dedupeKey: "notification:test:route:1:channel:2:fixture",
  status: "queued",
  jobId: null,
  availableAt: "2026-08-11T00:00:00Z",
  lastError: null,
  providerRef: null,
  createdAt: "2026-08-11T00:00:00Z",
  updatedAt: "2026-08-11T00:00:00Z",
  sentAt: null,
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("notification delivery settings HTTP contracts", () => {
  it("owns the eight settings/delivery methods in its dedicated adapter", async () => {
    const { createNotificationDeliverySettingsApi } = await import(
      "../../src/api/notifications"
    );
    const api = createNotificationDeliverySettingsApi(() => new Error("fixture"), "p");

    expect(Object.keys(api).sort()).toEqual([
      "createNotificationChannel",
      "createNotificationRoute",
      "listNotificationChannels",
      "listNotificationDeliveryRequests",
      "listNotificationRoutes",
      "patchNotificationChannel",
      "patchNotificationRoute",
      "testNotificationRoute",
    ]);
  });

  it.each([
    ["list channels", () => realApi.listNotificationChannels(), "GET", "/api/projects/p/notification-channels", undefined, { schema_version: "frisket.notification_channels.v1", channels: [channel] }, { schemaVersion: "frisket.notification_channels.v1", channels: [domainChannel] }],
    ["create channel", () => realApi.createNotificationChannel({ kind: "email", name: "Ops", from: "ops@example.com" }), "POST", "/api/projects/p/notification-channels", { kind: "email", name: "Ops", from: "ops@example.com" }, channel, domainChannel],
    ["patch channel", () => realApi.updateNotificationChannel(2, { enabled: false }), "PATCH", "/api/projects/p/notification-channels/2", { enabled: false }, channel, domainChannel],
    ["list routes", () => realApi.listNotificationRoutes(), "GET", "/api/projects/p/notification-routes", undefined, { schema_version: "frisket.notification_routes.v1", routes: [route] }, { schemaVersion: "frisket.notification_routes.v1", routes: [domainRoute] }],
    ["create route", () => realApi.createNotificationRoute({ name: "Watch", channelId: 2 }), "POST", "/api/projects/p/notification-routes", { name: "Watch", channel_id: 2 }, route, domainRoute],
    ["patch route", () => realApi.updateNotificationRoute(1, { sourceRefMatch: { watch_id: 1 } }), "PATCH", "/api/projects/p/notification-routes/1", { source_ref_match: { watch_id: 1 } }, route, domainRoute],
    ["test route", () => realApi.testNotificationRoute(1, "ignored"), "POST", "/api/projects/p/notification-routes/1/test", undefined, delivery, domainDelivery],
    ["list delivery requests", () => realApi.listNotificationDeliveryRequests({ status: "queued", routeId: 1, channelId: 2, notificationId: 7 }), "GET", "/api/projects/p/notification-delivery-requests?offset=0&limit=20&status=queued&route_id=1&channel_id=2&notification_id=7", undefined, { schema_version: "frisket.notification_delivery_requests.v1", order: "desc", offset: 0, limit: 20, total: 1, has_more: false, next_offset: null, delivery_requests: [delivery] }, { schemaVersion: "frisket.notification_delivery_requests.v1", order: "desc", offset: 0, limit: 20, total: 1, hasMore: false, nextOffset: null, deliveryRequests: [domainDelivery] }],
  ])("uses generated transport and maps RealApi %s", async (_name, invoke, method, path, body, response, expected) => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    const result = await invoke();

    expect(result).toEqual(expected);
    expect(fetch).toHaveBeenCalledWith(
      path,
      expect.objectContaining({
        method,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      }),
    );
  });

  it("maps and filters reconciliation-required deliveries", async () => {
    const response = {
      schema_version: "frisket.notification_delivery_requests.v1",
      order: "desc",
      offset: 0,
      limit: 20,
      total: 1,
      has_more: false,
      next_offset: null,
      delivery_requests: [{ ...delivery, status: "reconciliation_required" }],
    };
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(response), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetch);

    const result = await realApi.listNotificationDeliveryRequests({
      status: "reconciliation_required",
    });

    expect(result.deliveryRequests[0]?.status).toBe("reconciliation_required");
    expect(fetch).toHaveBeenCalledWith(
      "/api/projects/p/notification-delivery-requests?offset=0&limit=20&status=reconciliation_required",
      expect.objectContaining({ method: "GET" }),
    );
  });
});
