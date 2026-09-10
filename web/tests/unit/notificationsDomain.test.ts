import { afterEach, describe, expect, it, vi } from "vitest";
import type { NotificationsDomainApi } from "../../src/api/notifications";

type DomainFactory = (
  errorFactory: (status: number, payload: unknown) => Error,
  projectId: string,
) => NotificationsDomainApi;

interface NotificationsModule {
  createNotificationsDomainApi?: DomainFactory;
}

interface Call {
  operationId: string;
  options: { query?: Record<string, unknown>; body?: unknown };
}

const page = {
  offset: 0, limit: 50, total: 1, has_more: false, next_offset: null,
  notifications: [{
    id: 1, source_kind: "watch", source_ref: { watch_id: 5 },
    source_event_ids: [2], event_count: 1, event_kinds: ["row_entered"],
    title: "Budget", summary: "Changed", severity: "warning", deep_link: {},
    created_at: "2026-08-11 00:00:00", updated_at: "2026-08-11 00:00:01",
    state: "unseen", seen_at: null, read_at: null, acknowledged_at: null,
    acknowledged_by: null,
  }],
};
const actor = {
  notification_id: 1, actor_id: "local:project", state: "read", seen_at: null,
  read_at: "2026-08-11 00:00:02", acknowledged_at: null, acknowledged_by: null,
  updated_at: "2026-08-11 00:00:02",
};
const channel = {
  id: 2, kind: "email", name: "Ops", enabled: true, config: {}, has_secret: false,
  created_at: "2026-08-11 00:00:00", updated_at: "2026-08-11 00:00:00",
};
const route = {
  id: 1, name: "Watch", enabled: true, owner_kind: "project", owner_ref: "p",
  recipient_actor_id: null, channel_id: 2, source_kind: null, source_ref_match: {},
  event_kinds: [], severity_min: "info", delivery_mode: "immediate",
  digest_cadence: null, digest_timezone: "UTC", digest_anchor_time: null,
  template_key: null, created_at: "2026-08-11 00:00:00", updated_at: "2026-08-11 00:00:00",
};
const delivery = {
  id: 7, route_id: 1, channel_id: 2, notification_id: null, digest_run_id: null,
  delivery_kind: "test", dedupe_key: "test", status: "queued", job_id: null,
  available_at: "2026-08-11 00:00:00", last_error: null, provider_ref: null,
  created_at: "2026-08-11 00:00:00", updated_at: "2026-08-11 00:00:00", sent_at: null,
};

function response(operationId: string): unknown {
  if (operationId === "tenant.list_notifications.get") return page;
  if (operationId === "tenant.notifications_summary.get") {
    return { total: 1, unseen: 1, seen: 0, read: 0, acknowledged: 0 };
  }
  if (operationId === "tenant.mark_notifications_seen.post") return { seen_count: 1 };
  if (operationId === "tenant.test_notification_route.post") return delivery;
  if (operationId.includes("notification_channel")) {
    return operationId.startsWith("tenant.list") ? { channels: [channel] } : channel;
  }
  if (operationId.includes("notification_route")) {
    return operationId.startsWith("tenant.list") ? { routes: [route] } : route;
  }
  if (operationId === "tenant.list_notification_delivery_requests.get") {
    return {
      offset: 0, limit: 20, total: 1, has_more: false, next_offset: null,
      delivery_requests: [delivery],
    };
  }
  return actor;
}

async function loadDomain(): Promise<NotificationsModule> {
  const path = "../../src/api/notifications";
  return import(/* @vite-ignore */ path) as Promise<NotificationsModule>;
}

afterEach(() => {
  vi.doUnmock("../../src/api/httpContract");
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("notifications domain", () => {
  it("directly composes generated operations with defaults, projections, and legacy omissions", async () => {
    const calls: Call[] = [];
    vi.resetModules();
    vi.doMock("../../src/api/httpContract", () => ({
      httpContract: (operationId: string, options: Call["options"]) => {
        calls.push({ operationId, options });
        return Promise.resolve(response(operationId));
      },
    }));
    const module = await loadDomain();
    expect(
      module.createNotificationsDomainApi,
      "INTENDED_RED: notifications domain factory must be exported",
    ).toBeDefined();
    if (!module.createNotificationsDomainApi) return;
    const api = module.createNotificationsDomainApi((status, payload) =>
      new Error(`${status}:${String(payload)}`),
      "notification-domain",
    );

    const defaultPage = await api.listNotifications();
    const mappedPage = await api.listNotifications({
      state: "unseen", sourceKind: "watch", severity: "warning", offset: 2, limit: 3,
      sourceRef: { watch_id: 5, nested: { keep: true, omit: undefined }, array: [undefined] },
    });
    const summary = await api.getNotificationsSummary();
    const seen = await api.markNotificationsSeen({
      sourceRef: { nested: { keep: true, omit: undefined }, array: [undefined] },
    });
    const read = await api.markNotificationRead(1);
    await api.ackNotification(1);
    await api.unackNotification(1);
    const channels = await api.listNotificationChannels();
    await api.createNotificationChannel({ kind: "email", name: "Ops", from: "ops@example.com" });
    await api.updateNotificationChannel(2, { enabled: false });
    const routes = await api.listNotificationRoutes();
    await api.createNotificationRoute({
      name: "Watch", channelId: 2, sourceKind: null, digestCadence: null, digestAnchorTime: null,
    });
    await api.updateNotificationRoute(1, { sourceRefMatch: { watch_id: 1 } });
    const tested = await api.testNotificationRoute(1, "ignored");
    const deliveries = await api.listNotificationDeliveryRequests({ status: "queued" });

    expect(defaultPage).toMatchObject({ schemaVersion: "frisket.notifications_page.v1", order: "desc", offset: 0, limit: 50 });
    expect(mappedPage.notifications[0]).toMatchObject({ sourceKind: "watch", createdAt: "2026-08-11T00:00:00Z", sourceRef: { watch_id: 5 } });
    expect(summary).toMatchObject({ schemaVersion: "frisket.notifications_summary.v1", bySeverity: {}, bySourceKind: {} });
    expect(seen).toEqual({ seenCount: 1 });
    expect(read).toMatchObject({ notificationId: 1, readAt: "2026-08-11T00:00:02Z" });
    expect(channels.channels[0]).toMatchObject({ ownerKind: "project", createdAt: "2026-08-11T00:00:00Z" });
    expect(routes.routes[0]).toMatchObject({ ownerKind: "project", channelId: 2, digestCadence: null });
    expect(tested).toMatchObject({ notificationId: null, sentAt: null });
    expect(deliveries).toMatchObject({ schemaVersion: "frisket.notification_delivery_requests.v1", order: "desc", offset: 0, limit: 20 });
    expect(calls.map((call) => call.operationId)).toEqual([
      "tenant.list_notifications.get", "tenant.list_notifications.get",
      "tenant.notifications_summary.get", "tenant.mark_notifications_seen.post",
      "tenant.mark_notification_read.post", "tenant.ack_notification.post", "tenant.unack_notification.post",
      "tenant.list_notification_channels.get", "tenant.create_notification_channel.post", "tenant.patch_notification_channel.patch",
      "tenant.list_notification_routes.get", "tenant.create_notification_route.post", "tenant.patch_notification_route.patch",
      "tenant.test_notification_route.post", "tenant.list_notification_delivery_requests.get",
    ]);
    expect(calls[0].options.query).toMatchObject({ state: "all", offset: 0, limit: 50 });
    expect(calls[1].options.query).toMatchObject({
      state: "unseen", offset: 2, limit: 3, source_kind: "watch", severity: "warning",
      source_ref: '{"watch_id":5,"nested":{"keep":true},"array":[null]}',
    });
    expect(calls[3].options.body).toEqual({ source_ref: { nested: { keep: true }, array: [null] } });
    expect(calls.slice(4, 7).map((call) => call.options.body)).toEqual([undefined, undefined, undefined]);
    expect(calls[11].options.body).toEqual({ name: "Watch", channel_id: 2, source_kind: null, digest_cadence: null, digest_anchor_time: null });
    expect(calls[13].options.body).toBeUndefined();
    expect(calls[14].options.query).toMatchObject({ offset: 0, limit: 20, status: "queued" });
  });

  it("uses the supplied contract error factory", async () => {
    const module = await loadDomain();
    expect(module.createNotificationsDomainApi).toBeDefined();
    if (!module.createNotificationsDomainApi) return;
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "bad" }), { status: 400 })));
    const error = await module.createNotificationsDomainApi(
      (status, payload) => Object.assign(new Error("mapped"), { status, payload }),
      "notification-error",
    ).markNotificationsSeen({}).catch((caught: unknown) => caught);
    expect(error).toMatchObject({ message: "mapped", status: 400, payload: { detail: "bad" } });
  });
});
