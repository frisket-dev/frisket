import type { HttpContractOperationMap } from "../generated/openHttpContracts";
import { httpContract, type HttpContractSuccessResponse } from "./httpContract";
import { toWireJsonObject } from "./viewsLenses";
import type {
  NotificationActorState,
  NotificationChannel,
  NotificationChannelInput,
  NotificationChannelsPage,
  NotificationDeliveryRequest,
  NotificationDeliveryRequestsPage,
  NotificationItem,
  NotificationListParams,
  NotificationPage,
  NotificationRoute,
  NotificationRouteInput,
  NotificationRoutesPage,
  NotificationStateFilter,
  NotificationSummary,
} from "./types";
import { sqliteTimestampToIso } from './wireValues';

type ContractRequestBody<Id extends keyof HttpContractOperationMap> = Exclude<
  HttpContractOperationMap[Id]["request"],
  undefined
>;

type NotificationPageWire =
  HttpContractSuccessResponse<"tenant.list_notifications.get">;
type NotificationItemWire = NotificationPageWire["notifications"][number];
type NotificationSummaryWire =
  HttpContractSuccessResponse<"tenant.notifications_summary.get">;
type NotificationActorStateWire =
  HttpContractSuccessResponse<"tenant.mark_notification_read.post">;
type NotificationChannelWire =
  HttpContractSuccessResponse<"tenant.create_notification_channel.post">;
type NotificationChannelsPageWire =
  HttpContractSuccessResponse<"tenant.list_notification_channels.get">;
type NotificationRouteWire =
  HttpContractSuccessResponse<"tenant.create_notification_route.post">;
type NotificationRoutesPageWire =
  HttpContractSuccessResponse<"tenant.list_notification_routes.get">;
type NotificationDeliveryRequestWire =
  HttpContractSuccessResponse<"tenant.test_notification_route.post">;
type NotificationDeliveryRequestsPageWire =
  HttpContractSuccessResponse<"tenant.list_notification_delivery_requests.get">;

type NotificationFilterBody =
  ContractRequestBody<"tenant.mark_notifications_seen.post">;
type NotificationChannelBody =
  ContractRequestBody<"tenant.create_notification_channel.post">;
type NotificationRouteBody =
  ContractRequestBody<"tenant.create_notification_route.post">;

const toNotificationItem = (wire: NotificationItemWire): NotificationItem => ({
  id: wire.id,
  sourceKind: wire.source_kind,
  sourceRef: wire.source_ref ?? {},
  sourceEventIds: wire.source_event_ids ?? [],
  eventCount: wire.event_count,
  eventKinds: wire.event_kinds ?? [],
  title: wire.title,
  summary: wire.summary,
  severity: wire.severity,
  deepLink: wire.deep_link ?? {},
  createdAt: sqliteTimestampToIso(wire.created_at),
  updatedAt: sqliteTimestampToIso(wire.updated_at),
  state: wire.state,
  seenAt: wire.seen_at ? sqliteTimestampToIso(wire.seen_at) : null,
  readAt: wire.read_at ? sqliteTimestampToIso(wire.read_at) : null,
  acknowledgedAt: wire.acknowledged_at ? sqliteTimestampToIso(wire.acknowledged_at) : null,
  acknowledgedBy: wire.acknowledged_by,
});

const toNotificationPage = (wire: NotificationPageWire): NotificationPage => ({
  schemaVersion: "frisket.notifications_page.v1",
  order: "desc",
  offset: wire.offset,
  limit: wire.limit,
  total: wire.total,
  hasMore: wire.has_more,
  nextOffset: wire.next_offset,
  notifications: wire.notifications.map(toNotificationItem),
});

const toNotificationSummary = (wire: NotificationSummaryWire): NotificationSummary => ({
  schemaVersion: "frisket.notifications_summary.v1",
  total: wire.total,
  unseen: wire.unseen,
  seen: wire.seen,
  read: wire.read,
  acknowledged: wire.acknowledged,
  bySeverity: wire.by_severity ?? {},
  bySourceKind: wire.by_source_kind ?? {},
  bySourceRef: (wire.by_source_ref ?? []).map((item) => ({
    sourceKind: item.source_kind,
    sourceRef: item.source_ref ?? {},
    unseen: item.unseen,
  })),
});

const toNotificationActorState = (
  wire: NotificationActorStateWire,
): NotificationActorState => ({
  notificationId: wire.notification_id,
  actorId: wire.actor_id,
  state: wire.state,
  seenAt: wire.seen_at ? sqliteTimestampToIso(wire.seen_at) : null,
  readAt: wire.read_at ? sqliteTimestampToIso(wire.read_at) : null,
  acknowledgedAt: wire.acknowledged_at ? sqliteTimestampToIso(wire.acknowledged_at) : null,
  acknowledgedBy: wire.acknowledged_by,
  updatedAt: sqliteTimestampToIso(wire.updated_at),
});

const toNotificationChannel = (wire: NotificationChannelWire): NotificationChannel => ({
  id: wire.id,
  kind: wire.kind,
  name: wire.name,
  enabled: Boolean(wire.enabled),
  ownerKind: wire.owner_kind ?? "project",
  config: wire.config,
  hasSecret: Boolean(wire.has_secret),
  createdAt: sqliteTimestampToIso(wire.created_at),
  updatedAt: sqliteTimestampToIso(wire.updated_at),
});

const toNotificationChannelsPage = (
  wire: NotificationChannelsPageWire,
): NotificationChannelsPage => ({
  schemaVersion: "frisket.notification_channels.v1",
  channels: wire.channels.map(toNotificationChannel),
});

const toNotificationRoute = (wire: NotificationRouteWire): NotificationRoute => ({
  id: wire.id,
  name: wire.name,
  enabled: Boolean(wire.enabled),
  ownerKind: wire.owner_kind,
  ownerRef: wire.owner_ref,
  recipientActorId: wire.recipient_actor_id,
  channelId: wire.channel_id,
  sourceKind: wire.source_kind,
  sourceRefMatch: wire.source_ref_match,
  eventKinds: wire.event_kinds,
  severityMin: wire.severity_min,
  deliveryMode: wire.delivery_mode,
  digestCadence: wire.digest_cadence,
  digestTimezone: wire.digest_timezone,
  digestAnchorTime: wire.digest_anchor_time,
  templateKey: wire.template_key,
  createdAt: sqliteTimestampToIso(wire.created_at),
  updatedAt: sqliteTimestampToIso(wire.updated_at),
});

const toNotificationRoutesPage = (
  wire: NotificationRoutesPageWire,
): NotificationRoutesPage => ({
  schemaVersion: "frisket.notification_routes.v1",
  routes: wire.routes.map(toNotificationRoute),
});

const toNotificationDeliveryRequest = (
  wire: NotificationDeliveryRequestWire,
): NotificationDeliveryRequest => ({
  id: wire.id,
  routeId: wire.route_id,
  channelId: wire.channel_id,
  notificationId: wire.notification_id,
  digestRunId: wire.digest_run_id,
  deliveryKind: wire.delivery_kind,
  dedupeKey: wire.dedupe_key,
  status: wire.status,
  jobId: wire.job_id,
  availableAt: sqliteTimestampToIso(wire.available_at),
  lastError: wire.last_error,
  providerRef: wire.provider_ref,
  createdAt: sqliteTimestampToIso(wire.created_at),
  updatedAt: sqliteTimestampToIso(wire.updated_at),
  sentAt: wire.sent_at ? sqliteTimestampToIso(wire.sent_at) : null,
});

const toNotificationDeliveryRequestsPage = (
  wire: NotificationDeliveryRequestsPageWire,
): NotificationDeliveryRequestsPage => ({
  schemaVersion: "frisket.notification_delivery_requests.v1",
  order: "desc",
  offset: wire.offset,
  limit: wire.limit,
  total: wire.total,
  hasMore: wire.has_more,
  nextOffset: wire.next_offset,
  deliveryRequests: wire.delivery_requests.map(toNotificationDeliveryRequest),
});

function toWireNotificationChannelInput(
  input: Partial<NotificationChannelInput>,
): NotificationChannelBody {
  const out: Record<string, unknown> = {};
  if (input.kind !== undefined) out.kind = input.kind;
  if (input.name !== undefined) out.name = input.name;
  if (input.ownerKind !== undefined) out.owner_kind = input.ownerKind;
  if (input.enabled !== undefined) out.enabled = input.enabled;
  if (input.to !== undefined) out.to = input.to;
  if (input.from !== undefined) out.from = input.from;
  if (input.channelLabel !== undefined) out.channel_label = input.channelLabel;
  if (input.webhookHost !== undefined) out.webhook_host = input.webhookHost;
  if (input.webhookUrlSecretRef !== undefined) out.webhook_url_secret_ref = input.webhookUrlSecretRef;
  if (input.signingSecretRef !== undefined) out.signing_secret_ref = input.signingSecretRef;
  if (input.signatureHeader !== undefined) out.signature_header = input.signatureHeader;
  if (input.maxRequestBytes !== undefined) out.max_request_bytes = input.maxRequestBytes;
  if (input.maxResponseBytes !== undefined) out.max_response_bytes = input.maxResponseBytes;
  if (input.maxRedirects !== undefined) out.max_redirects = input.maxRedirects;
  if (input.secretRef !== undefined) out.secret_ref = input.secretRef;
  if (input.webhookSecretRef !== undefined) out.webhook_secret_ref = input.webhookSecretRef;
  return out as NotificationChannelBody;
}

function toWireNotificationRouteInput(
  input: Partial<NotificationRouteInput>,
): NotificationRouteBody {
  const out: Record<string, unknown> = {};
  if (input.name !== undefined) out.name = input.name;
  if (input.ownerKind !== undefined) out.owner_kind = input.ownerKind;
  if (input.enabled !== undefined) out.enabled = input.enabled;
  if (input.channelId !== undefined) out.channel_id = input.channelId;
  if (input.sourceKind !== undefined) out.source_kind = input.sourceKind;
  if (input.sourceRefMatch !== undefined) out.source_ref_match = input.sourceRefMatch;
  if (input.eventKinds !== undefined) out.event_kinds = input.eventKinds;
  if (input.severityMin !== undefined) out.severity_min = input.severityMin;
  if (input.deliveryMode !== undefined) out.delivery_mode = input.deliveryMode;
  if (input.digestCadence !== undefined) out.digest_cadence = input.digestCadence;
  if (input.digestTimezone !== undefined) out.digest_timezone = input.digestTimezone;
  if (input.digestAnchorTime !== undefined) out.digest_anchor_time = input.digestAnchorTime;
  return out as NotificationRouteBody;
}

function toWireNotificationFilter(input: NotificationStateFilter): NotificationFilterBody {
  const out: NotificationFilterBody = {};
  if (input.notificationIds !== undefined) out.notification_ids = input.notificationIds;
  if (input.sourceKind !== undefined) out.source_kind = input.sourceKind;
  if (input.sourceRef !== undefined) out.source_ref = toWireJsonObject(input.sourceRef);
  if (input.beforeCreatedAt !== undefined) out.before_created_at = input.beforeCreatedAt;
  return out;
}

export function createNotificationsApi(projectId: string) {
  const project = { pid: projectId };
  return {
    async listNotifications(params: NotificationListParams = {}) {
      return httpContract("tenant.list_notifications.get", {
        pathParams: project,
        // Keep the established query order.
        query: {
          state: params.state ?? "all",
          offset: params.offset ?? 0,
          limit: params.limit ?? 50,
          source_kind: params.sourceKind || undefined,
          source_ref: params.sourceRef
            ? JSON.stringify(toWireJsonObject(params.sourceRef))
            : undefined,
          severity: params.severity || undefined,
        },
      }, toNotificationPage);
    },

    async getNotificationsSummary() {
      return httpContract(
        "tenant.notifications_summary.get",
        { pathParams: project },
        toNotificationSummary,
      );
    },

    async markNotificationsSeen(filter: NotificationStateFilter) {
      return httpContract(
        "tenant.mark_notifications_seen.post",
        { pathParams: project, body: toWireNotificationFilter(filter) },
        (wire) => ({ seenCount: wire.seen_count }),
      );
    },

    /** The three per-item POSTs are bodyless: the routes declare no request body. */
    async markNotificationRead(notificationId: number) {
      return httpContract(
        "tenant.mark_notification_read.post",
        { pathParams: { ...project, notification_id: notificationId } },
        toNotificationActorState,
      );
    },

    async ackNotification(notificationId: number) {
      return httpContract(
        "tenant.ack_notification.post",
        { pathParams: { ...project, notification_id: notificationId } },
        toNotificationActorState,
      );
    },

    async unackNotification(notificationId: number) {
      return httpContract(
        "tenant.unack_notification.post",
        { pathParams: { ...project, notification_id: notificationId } },
        toNotificationActorState,
      );
    },

    async listNotificationChannels() {
      return httpContract(
        "tenant.list_notification_channels.get",
        { pathParams: project },
        toNotificationChannelsPage,
      );
    },

    async createNotificationChannel(input: NotificationChannelInput) {
      return httpContract(
        "tenant.create_notification_channel.post",
        { pathParams: project, body: toWireNotificationChannelInput(input) },
        toNotificationChannel,
      );
    },

    async updateNotificationChannel(
      channelId: number,
      input: Partial<NotificationChannelInput>,
    ) {
      return httpContract(
        "tenant.patch_notification_channel.patch",
        {
          pathParams: { ...project, channel_id: channelId },
          body: toWireNotificationChannelInput(input),
        },
        toNotificationChannel,
      );
    },

    async listNotificationRoutes() {
      return httpContract(
        "tenant.list_notification_routes.get",
        { pathParams: project },
        toNotificationRoutesPage,
      );
    },

    async createNotificationRoute(input: NotificationRouteInput) {
      return httpContract(
        "tenant.create_notification_route.post",
        { pathParams: project, body: toWireNotificationRouteInput(input) },
        toNotificationRoute,
      );
    },

    async updateNotificationRoute(
      routeId: number,
      input: Partial<NotificationRouteInput>,
    ) {
      return httpContract(
        "tenant.patch_notification_route.patch",
        {
          pathParams: { ...project, route_id: routeId },
          body: toWireNotificationRouteInput(input),
        },
        toNotificationRoute,
      );
    },

    async testNotificationRoute(routeId: number, ownerKind?: string) {
      void ownerKind;
      return httpContract(
        "tenant.test_notification_route.post",
        { pathParams: { ...project, route_id: routeId } },
        toNotificationDeliveryRequest,
      );
    },

    async listNotificationDeliveryRequests(params: {
      status?: NotificationDeliveryRequest["status"];
      routeId?: number;
      channelId?: number;
      notificationId?: number;
      offset?: number;
      limit?: number;
    } = {}) {
      return httpContract("tenant.list_notification_delivery_requests.get", {
        pathParams: project,
        // Keep the established query order and explicit client defaults.
        query: {
          offset: params.offset ?? 0,
          limit: params.limit ?? 20,
          status: params.status ?? undefined,
          route_id: params.routeId ?? undefined,
          channel_id: params.channelId ?? undefined,
          notification_id: params.notificationId ?? undefined,
        },
      }, toNotificationDeliveryRequestsPage);
    },
  };
}
