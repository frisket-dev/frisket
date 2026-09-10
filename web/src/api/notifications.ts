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

export interface NotificationsOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

type ContractRequestBody<Id extends keyof HttpContractOperationMap> = Exclude<
  HttpContractOperationMap[Id]["request"],
  undefined
>;

export type NotificationPageWire =
  HttpContractSuccessResponse<"tenant.list_notifications.get">;
export type NotificationItemWire = NotificationPageWire["notifications"][number];
export type NotificationSummaryWire =
  HttpContractSuccessResponse<"tenant.notifications_summary.get">;
export type NotificationSeenCountWire =
  HttpContractSuccessResponse<"tenant.mark_notifications_seen.post">;
export type NotificationActorStateWire =
  HttpContractSuccessResponse<"tenant.mark_notification_read.post">;
export type NotificationChannelWire =
  HttpContractSuccessResponse<"tenant.create_notification_channel.post">;
export type NotificationChannelsPageWire =
  HttpContractSuccessResponse<"tenant.list_notification_channels.get">;
export type NotificationRouteWire =
  HttpContractSuccessResponse<"tenant.create_notification_route.post">;
export type NotificationRoutesPageWire =
  HttpContractSuccessResponse<"tenant.list_notification_routes.get">;
export type NotificationDeliveryRequestWire =
  HttpContractSuccessResponse<"tenant.test_notification_route.post">;
export type NotificationDeliveryRequestsPageWire =
  HttpContractSuccessResponse<"tenant.list_notification_delivery_requests.get">;

export type NotificationFilterBody =
  ContractRequestBody<"tenant.mark_notifications_seen.post">;
export type NotificationChannelBody =
  ContractRequestBody<"tenant.create_notification_channel.post">;
export type NotificationRouteBody =
  ContractRequestBody<"tenant.create_notification_route.post">;

export interface NotificationsApi {
  listNotifications(
    state?: string | null,
    sourceKind?: string | null,
    sourceRef?: string | null,
    severity?: string | null,
    offset?: number | null,
    limit?: number | null,
    options?: NotificationsOptions,
  ): Promise<NotificationPageWire>;
  notificationsSummary(
    options?: NotificationsOptions,
  ): Promise<NotificationSummaryWire>;
  markNotificationsSeen(
    body: NotificationFilterBody,
    options?: NotificationsOptions,
  ): Promise<NotificationSeenCountWire>;
  /** The three per-item POSTs are BODYLESS: the routes declare no request
   *  body, so none rides the wire. (Base's vestigial `{}` bodies are the
   *  recorded client-side wire delta of this cutover — the F6 runWatch
   *  precedent.) */
  markNotificationRead(
    notificationId: number,
    options?: NotificationsOptions,
  ): Promise<NotificationActorStateWire>;
  ackNotification(
    notificationId: number,
    options?: NotificationsOptions,
  ): Promise<NotificationActorStateWire>;
  unackNotification(
    notificationId: number,
    options?: NotificationsOptions,
  ): Promise<NotificationActorStateWire>;
}

/** Channel, route, and delivery-request operations are deliberately isolated
 * from notification reads so the established read adapter retains its exact
 * seven-method contract. */
export interface NotificationDeliverySettingsApi {
  listNotificationChannels(
    options?: NotificationsOptions,
  ): Promise<NotificationChannelsPageWire>;
  createNotificationChannel(
    body: NotificationChannelBody,
    options?: NotificationsOptions,
  ): Promise<NotificationChannelWire>;
  patchNotificationChannel(
    channelId: number,
    body: NotificationChannelBody,
    options?: NotificationsOptions,
  ): Promise<NotificationChannelWire>;
  listNotificationRoutes(
    options?: NotificationsOptions,
  ): Promise<NotificationRoutesPageWire>;
  createNotificationRoute(
    body: NotificationRouteBody,
    options?: NotificationsOptions,
  ): Promise<NotificationRouteWire>;
  patchNotificationRoute(
    routeId: number,
    body: NotificationRouteBody,
    options?: NotificationsOptions,
  ): Promise<NotificationRouteWire>;
  testNotificationRoute(
    routeId: number,
    options?: NotificationsOptions,
  ): Promise<NotificationDeliveryRequestWire>;
  listNotificationDeliveryRequests(
    status?: string | null,
    routeId?: number | null,
    channelId?: number | null,
    notificationId?: number | null,
    offset?: number | null,
    limit?: number | null,
    options?: NotificationsOptions,
  ): Promise<NotificationDeliveryRequestsPageWire>;
}

/** The feature-facing port. The generated read and delivery adapters stay
 * private to this module so real.ts composes one notification domain owner
 * and delegates all notification behavior to it. */
export interface NotificationsDomainApi {
  listNotifications(params?: NotificationListParams): Promise<NotificationPage>;
  getNotificationsSummary(): Promise<NotificationSummary>;
  markNotificationsSeen(filter: NotificationStateFilter): Promise<{ seenCount: number }>;
  markNotificationRead(notificationId: number): Promise<NotificationActorState>;
  ackNotification(notificationId: number): Promise<NotificationActorState>;
  unackNotification(notificationId: number): Promise<NotificationActorState>;
  listNotificationChannels(): Promise<NotificationChannelsPage>;
  createNotificationChannel(input: NotificationChannelInput): Promise<NotificationChannel>;
  updateNotificationChannel(
    channelId: number,
    input: Partial<NotificationChannelInput>,
  ): Promise<NotificationChannel>;
  listNotificationRoutes(): Promise<NotificationRoutesPage>;
  createNotificationRoute(input: NotificationRouteInput): Promise<NotificationRoute>;
  updateNotificationRoute(
    routeId: number,
    input: Partial<NotificationRouteInput>,
  ): Promise<NotificationRoute>;
  testNotificationRoute(
    routeId: number,
    ownerKind?: string,
  ): Promise<NotificationDeliveryRequest>;
  listNotificationDeliveryRequests(params?: {
    status?: NotificationDeliveryRequest["status"];
    routeId?: number;
    channelId?: number;
    notificationId?: number;
    offset?: number;
    limit?: number;
  }): Promise<NotificationDeliveryRequestsPage>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

export function createNotificationsApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): NotificationsApi {
  return {
    listNotifications(state, sourceKind, sourceRef, severity, offset, limit, options = {}) {
      return httpContract(
        "tenant.list_notifications.get",
        {
          pathParams: { pid: projectId },
          // Keep the established query order.
          query: {
            state: state ?? undefined,
            offset: offset ?? undefined,
            limit: limit ?? undefined,
            source_kind: sourceKind ?? undefined,
            source_ref: sourceRef ?? undefined,
            severity: severity ?? undefined,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    notificationsSummary(options = {}) {
      return httpContract(
        "tenant.notifications_summary.get",
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    markNotificationsSeen(body, options = {}) {
      return httpContract(
        "tenant.mark_notifications_seen.post",
        {
          pathParams: { pid: projectId },
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    markNotificationRead(notificationId, options = {}) {
      return httpContract(
        "tenant.mark_notification_read.post",
        {
          pathParams: { pid: projectId, notification_id: notificationId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    ackNotification(notificationId, options = {}) {
      return httpContract(
        "tenant.ack_notification.post",
        {
          pathParams: { pid: projectId, notification_id: notificationId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    unackNotification(notificationId, options = {}) {
      return httpContract(
        "tenant.unack_notification.post",
        {
          pathParams: { pid: projectId, notification_id: notificationId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}

export function createNotificationDeliverySettingsApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): NotificationDeliverySettingsApi {
  return {
    listNotificationChannels(options = {}) {
      return httpContract(
        "tenant.list_notification_channels.get",
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    createNotificationChannel(body, options = {}) {
      return httpContract(
        "tenant.create_notification_channel.post",
        {
          pathParams: { pid: projectId },
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    patchNotificationChannel(channelId, body, options = {}) {
      return httpContract(
        "tenant.patch_notification_channel.patch",
        {
          pathParams: { pid: projectId, channel_id: channelId },
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    listNotificationRoutes(options = {}) {
      return httpContract(
        "tenant.list_notification_routes.get",
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    createNotificationRoute(body, options = {}) {
      return httpContract(
        "tenant.create_notification_route.post",
        {
          pathParams: { pid: projectId },
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    patchNotificationRoute(routeId, body, options = {}) {
      return httpContract(
        "tenant.patch_notification_route.patch",
        {
          pathParams: { pid: projectId, route_id: routeId },
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    testNotificationRoute(routeId, options = {}) {
      return httpContract(
        "tenant.test_notification_route.post",
        {
          pathParams: { pid: projectId, route_id: routeId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    listNotificationDeliveryRequests(
      status,
      routeId,
      channelId,
      notificationId,
      offset,
      limit,
      options = {},
    ) {
      return httpContract(
        "tenant.list_notification_delivery_requests.get",
        {
          pathParams: { pid: projectId },
          query: {
            // Preserve the handwritten byte order and explicit client defaults.
            offset: offset ?? 0,
            limit: limit ?? 20,
            status: status ?? undefined,
            route_id: routeId ?? undefined,
            channel_id: channelId ?? undefined,
            notification_id: notificationId ?? undefined,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}

/** sqlite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS", UTC) → ISO. */
const toNotificationIso = (timestamp: string): string =>
  timestamp.includes("T") ? timestamp : `${timestamp.replace(" ", "T")}Z`;

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
  createdAt: toNotificationIso(wire.created_at),
  updatedAt: toNotificationIso(wire.updated_at),
  state: wire.state,
  seenAt: wire.seen_at ? toNotificationIso(wire.seen_at) : null,
  readAt: wire.read_at ? toNotificationIso(wire.read_at) : null,
  acknowledgedAt: wire.acknowledged_at ? toNotificationIso(wire.acknowledged_at) : null,
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
  seenAt: wire.seen_at ? toNotificationIso(wire.seen_at) : null,
  readAt: wire.read_at ? toNotificationIso(wire.read_at) : null,
  acknowledgedAt: wire.acknowledged_at ? toNotificationIso(wire.acknowledged_at) : null,
  acknowledgedBy: wire.acknowledged_by,
  updatedAt: toNotificationIso(wire.updated_at),
});

const toNotificationChannel = (wire: NotificationChannelWire): NotificationChannel => ({
  id: wire.id,
  kind: wire.kind,
  name: wire.name,
  enabled: Boolean(wire.enabled),
  ownerKind: wire.owner_kind ?? "project",
  config: wire.config,
  hasSecret: Boolean(wire.has_secret),
  createdAt: toNotificationIso(wire.created_at),
  updatedAt: toNotificationIso(wire.updated_at),
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
  createdAt: toNotificationIso(wire.created_at),
  updatedAt: toNotificationIso(wire.updated_at),
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
  availableAt: toNotificationIso(wire.available_at),
  lastError: wire.last_error,
  providerRef: wire.provider_ref,
  createdAt: toNotificationIso(wire.created_at),
  updatedAt: toNotificationIso(wire.updated_at),
  sentAt: wire.sent_at ? toNotificationIso(wire.sent_at) : null,
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

export function createNotificationsDomainApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): NotificationsDomainApi {
  const reads = createNotificationsApi(errorFactory, projectId);
  const delivery = createNotificationDeliverySettingsApi(errorFactory, projectId);
  return {
    async listNotifications(params = {}) {
      return toNotificationPage(await reads.listNotifications(
        params.state ?? "all",
        params.sourceKind || null,
        params.sourceRef ? JSON.stringify(toWireJsonObject(params.sourceRef)) : null,
        params.severity || null,
        params.offset ?? 0,
        params.limit ?? 50,
      ));
    },

    async getNotificationsSummary() {
      return toNotificationSummary(await reads.notificationsSummary());
    },

    async markNotificationsSeen(filter) {
      const wire = await reads.markNotificationsSeen(toWireNotificationFilter(filter));
      return { seenCount: wire.seen_count };
    },

    async markNotificationRead(notificationId) {
      return toNotificationActorState(await reads.markNotificationRead(notificationId));
    },

    async ackNotification(notificationId) {
      return toNotificationActorState(await reads.ackNotification(notificationId));
    },

    async unackNotification(notificationId) {
      return toNotificationActorState(await reads.unackNotification(notificationId));
    },

    async listNotificationChannels() {
      return toNotificationChannelsPage(await delivery.listNotificationChannels());
    },

    async createNotificationChannel(input) {
      return toNotificationChannel(
        await delivery.createNotificationChannel(toWireNotificationChannelInput(input)),
      );
    },

    async updateNotificationChannel(channelId, input) {
      return toNotificationChannel(
        await delivery.patchNotificationChannel(
          channelId,
          toWireNotificationChannelInput(input),
        ),
      );
    },

    async listNotificationRoutes() {
      return toNotificationRoutesPage(await delivery.listNotificationRoutes());
    },

    async createNotificationRoute(input) {
      return toNotificationRoute(
        await delivery.createNotificationRoute(toWireNotificationRouteInput(input)),
      );
    },

    async updateNotificationRoute(routeId, input) {
      return toNotificationRoute(
        await delivery.patchNotificationRoute(routeId, toWireNotificationRouteInput(input)),
      );
    },

    async testNotificationRoute(routeId, ownerKind) {
      void ownerKind;
      return toNotificationDeliveryRequest(await delivery.testNotificationRoute(routeId));
    },

    async listNotificationDeliveryRequests(params = {}) {
      return toNotificationDeliveryRequestsPage(
        await delivery.listNotificationDeliveryRequests(
          params.status,
          params.routeId,
          params.channelId,
          params.notificationId,
          params.offset,
          params.limit,
        ),
      );
    },
  };
}
