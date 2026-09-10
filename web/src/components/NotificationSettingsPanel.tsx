import { type ReactNode, useCallback, useEffect, useMemo, useReducer } from 'react';
import { PanelEmpty, PanelError, StatusChip, type StatusTone } from './PanelPrimitives';
import { PanelSelect } from './PanelSelect';
import {
  Activity,
  BellRing,
  CheckCircle2,
  Hash,
  Mail,
  RefreshCw,
  Send,
  Settings,
  ToggleLeft,
  ToggleRight,
} from 'lucide-react';
import {
  type NotificationChannel,
  type NotificationDeliveryRequest,
  type NotificationDigestCadence,
  type NotificationRoute,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useEditionModule } from '../editions/module';

// Edition capabilities are read when the panel renders, after a downstream
// entrypoint has registered its descriptor. The notification probe itself is
// lazy and starts on the first store subscription (first panel mount).
type ChannelDraftKind = 'slack' | 'email';

interface ChannelDraft {
  kind: ChannelDraftKind;
  name: string;
  enabled: boolean;
  to: string;
  from: string;
  channelLabel: string;
  webhookHost: string;
  secretRef: string;
}

interface RouteDraft {
  name: string;
  channelId: string;
  enabled: boolean;
  sourceKind: 'all' | 'watch' | 'run' | 'source';
  watchId: string;
  severityMin: 'info' | 'warning' | 'critical';
  deliveryMode: 'immediate' | 'digest';
  digestCadence: NotificationDigestCadence;
  digestAnchorTime: string;
}


const initialChannelDraft: ChannelDraft = {
  kind: 'slack',
  name: 'Ops Slack',
  enabled: true,
  to: '',
  from: '',
  channelLabel: '#alerts',
  webhookHost: 'hooks.slack.com',
  secretRef: 'env:SLACK_WEBHOOK_URL',
};

const initialRouteDraft: RouteDraft = {
  name: 'Watch Slack immediate',
  channelId: '',
  enabled: true,
  sourceKind: 'watch',
  watchId: '',
  severityMin: 'info',
  deliveryMode: 'immediate',
  digestCadence: 'daily',
  digestAnchorTime: '09:00',
};

function channelLabel(channel: NotificationChannel): string {
  if (channel.kind === 'email') return `${channel.name} · ${String(channel.config.to ?? 'email')}`;
  if (channel.kind === 'slack') {
    return `${channel.name} · ${String(channel.config.channel_label ?? channel.config.webhook_host ?? 'Slack')}`;
  }
  if (channel.kind === 'webhook') {
    return `${channel.name} · ${String(channel.config.webhook_host ?? 'Webhook')}`;
  }
  return channel.name;
}

function routeModeLabel(route: NotificationRoute): string {
  if (route.deliveryMode === 'digest') {
    return `${route.digestCadence ?? 'daily'} digest at ${route.digestAnchorTime ?? '09:00'} UTC`;
  }
  return 'immediate';
}

function sourceMatchLabel(route: NotificationRoute): string {
  if (!route.sourceKind) return 'all notifications';
  const watchId = route.sourceRefMatch.watch_id;
  if (route.sourceKind === 'watch' && watchId !== undefined) return `watch_id: ${String(watchId)}`;
  return route.sourceKind;
}

function requestRouteLabel(request: NotificationDeliveryRequest, routes: NotificationRoute[]): string {
  const route = routes.find((candidate) => candidate.id === request.routeId);
  return route?.name ?? `route ${request.routeId ?? 'none'}`;
}

// Delivery status → StatusChip tone. The status word itself stays in the DOM
// text (uppercased only via CSS, StatusChip's data-caps hook) so it remains
// assertable.
function statusChipTone(status: string): StatusTone {
  switch (status) {
    case 'sent':
      return 'success';
    case 'failed':
      return 'error';
    case 'queued':
    case 'processing':
      return 'warning';
    default:
      return 'neutral';
  }
}

function routePayload(draft: RouteDraft) {
  const sourceRefMatch = draft.sourceKind === 'watch' && draft.watchId.trim()
    ? { watch_id: Number(draft.watchId.trim()) }
    : {};
  return {
    name: draft.name.trim(),
    enabled: draft.enabled,
    channelId: Number(draft.channelId),
    sourceKind: draft.sourceKind === 'all' ? null : draft.sourceKind,
    sourceRefMatch,
    severityMin: draft.severityMin,
    deliveryMode: draft.deliveryMode,
    digestCadence: draft.deliveryMode === 'digest' ? draft.digestCadence : null,
    digestTimezone: 'UTC',
    digestAnchorTime: draft.deliveryMode === 'digest' ? draft.digestAnchorTime : null,
  };
}

const routeDefaultNames = new Set(['', 'Watch Slack immediate', 'Watch daily email digest']);

function nextRouteNameForMode(currentName: string, deliveryMode: RouteDraft['deliveryMode']): string {
  if (!routeDefaultNames.has(currentName.trim())) return currentName;
  return deliveryMode === 'digest' ? 'Watch daily email digest' : 'Watch Slack immediate';
}

interface NotifState {
  channels: NotificationChannel[];
  routes: NotificationRoute[];
  requests: NotificationDeliveryRequest[];
  channelDraft: ChannelDraft;
  routeDraft: RouteDraft;
  busy: string | null;
  error: string | null;
  notice: string | null;
}
type NotifAction =
  | { type: 'load_start' }
  | { type: 'load_success'; channels: NotificationChannel[]; routes: NotificationRoute[]; requests: NotificationDeliveryRequest[] }
  | { type: 'load_error'; error: string }
  | { type: 'set_busy'; label: string | null }
  | { type: 'set_error'; error: string | null }
  | { type: 'set_notice'; notice: string }
  | { type: 'clear_channel_secret' }
  | { type: 'route_draft_maybe_set_channel'; channelId: string }
  | { type: 'set_channel_draft'; update: Partial<ChannelDraft> }
  | { type: 'set_channel_kind'; kind: ChannelDraftKind }
  | { type: 'set_route_draft'; update: Partial<RouteDraft> }
  | { type: 'set_route_mode'; mode: RouteDraft['deliveryMode'] };
function notifReducer(state: NotifState, action: NotifAction): NotifState {
  switch (action.type) {
    case 'load_start':
      return { ...state, busy: state.busy ?? 'load', error: null };
    case 'load_success': {
      const channels = action.channels;
      const autoChannel = !state.routeDraft.channelId && channels.length > 0
        ? channels.find((c) => c.kind !== 'in_app')
        : undefined;
      return {
        ...state,
        channels,
        routes: action.routes,
        requests: action.requests,
        busy: null,
        routeDraft: autoChannel ? { ...state.routeDraft, channelId: String(autoChannel.id) } : state.routeDraft,
      };
    }
    case 'load_error':
      return { ...state, error: action.error, busy: null };
    case 'set_busy':
      return { ...state, busy: action.label };
    case 'set_error':
      return { ...state, error: action.error };
    case 'set_notice':
      return { ...state, notice: action.notice };
    case 'clear_channel_secret':
      return { ...state, channelDraft: { ...state.channelDraft, secretRef: '' } };
    case 'route_draft_maybe_set_channel':
      if (state.routeDraft.channelId) return state;
      return { ...state, routeDraft: { ...state.routeDraft, channelId: action.channelId } };
    case 'set_channel_draft':
      return { ...state, channelDraft: { ...state.channelDraft, ...action.update } };
    case 'set_channel_kind':
      return {
        ...state,
        channelDraft: {
          ...state.channelDraft,
          kind: action.kind,
          name: action.kind === 'email' ? 'Ops email' : 'Ops Slack',
          secretRef: action.kind === 'email' ? 'env:RESEND_API_KEY' : 'env:SLACK_WEBHOOK_URL',
        },
      };
    case 'set_route_draft':
      return { ...state, routeDraft: { ...state.routeDraft, ...action.update } };
    case 'set_route_mode':
      return {
        ...state,
        routeDraft: {
          ...state.routeDraft,
          deliveryMode: action.mode,
          name: nextRouteNameForMode(state.routeDraft.name, action.mode),
        },
      };
  }
}
const notifInitial: NotifState = {
  channels: [], routes: [], requests: [],
  channelDraft: initialChannelDraft,
  routeDraft: initialRouteDraft,
  busy: 'load', error: null, notice: null,
};

export function NotificationSettingsPanel() {
  const { descriptor } = useEditionModule();
  const { projectApi: api } = useWorkspaceStores();
  const [state, dispatch] = useReducer(notifReducer, notifInitial);
  const capabilities = descriptor.capabilities;
  const identityMode = capabilities.identity;
  const configurableNotificationEmail =
    capabilities.configurableNotificationEmail;
  const externalChannels = useMemo(
    () => state.channels.filter((channel) => channel.kind !== 'in_app'),
    [state.channels],
  );
  const load = useCallback(() => {
    dispatch({ type: 'load_start' });
    void Promise.all([
      api.listNotificationChannels(),
      api.listNotificationRoutes(),
      api.listNotificationDeliveryRequests({ offset: 0, limit: 10 }),
    ])
      .then(([channelsPage, routesPage, requestsPage]) => {
        const hiddenEmailChannelIds = new Set(
          configurableNotificationEmail
            ? []
            : channelsPage.channels
                .filter((channel) => channel.kind === 'email')
                .map((channel) => channel.id),
        );
        dispatch({
          type: 'load_success',
          channels: channelsPage.channels.filter(
            (channel) => !hiddenEmailChannelIds.has(channel.id),
          ),
          routes: routesPage.routes.filter(
            (route) => !hiddenEmailChannelIds.has(route.channelId),
          ),
          requests: requestsPage.deliveryRequests.filter(
            (request) => !hiddenEmailChannelIds.has(request.channelId),
          ),
        });
      })
      .catch((e: Error) => dispatch({ type: 'load_error', error: e.message }));
  }, [configurableNotificationEmail]);
  useEffect(() => {
    const timer = window.setTimeout(load, 0);
    return () => window.clearTimeout(timer);
  }, [load]);
  const createChannel = useCallback((draft: ChannelDraft) => {
    dispatch({ type: 'set_busy', label: 'channel' });
    dispatch({ type: 'set_error', error: null });
    const input = draft.kind === 'email'
      ? { kind: 'email' as const, name: draft.name.trim(), ownerKind: identityMode ? 'user' : undefined, enabled: draft.enabled, to: draft.to.trim(), from: draft.from.trim() || undefined, secretRef: draft.secretRef.trim() || undefined }
      : { kind: 'slack' as const, name: draft.name.trim(), ownerKind: identityMode ? 'user' : undefined, enabled: draft.enabled, channelLabel: draft.channelLabel.trim(), webhookHost: draft.webhookHost.trim() || 'hooks.slack.com', webhookSecretRef: draft.secretRef.trim() || undefined };
    void api.createNotificationChannel(input)
      .then((channel) => {
        dispatch({ type: 'set_notice', notice: `${channel.name} channel saved` });
        dispatch({ type: 'clear_channel_secret' });
        dispatch({ type: 'route_draft_maybe_set_channel', channelId: String(channel.id) });
        load();
      })
      .catch((e: Error) => dispatch({ type: 'set_error', error: e.message }))
      .finally(() => dispatch({ type: 'set_busy', label: null }));
  }, [identityMode, load]);
  const createRoute = useCallback((draft: RouteDraft) => {
    dispatch({ type: 'set_busy', label: 'route' });
    dispatch({ type: 'set_error', error: null });
    void api.createNotificationRoute({ ...routePayload(draft), ownerKind: identityMode ? 'user' : undefined })
      .then((route) => { dispatch({ type: 'set_notice', notice: `${route.name} route saved` }); load(); })
      .catch((e: Error) => dispatch({ type: 'set_error', error: e.message }))
      .finally(() => dispatch({ type: 'set_busy', label: null }));
  }, [identityMode, load]);
  const toggleChannel = useCallback((channel: NotificationChannel) => {
    dispatch({ type: 'set_busy', label: `channel:${channel.id}` });
    void api.updateNotificationChannel(channel.id, { enabled: !channel.enabled, ownerKind: channel.ownerKind })
      .then(load)
      .catch((e: Error) => dispatch({ type: 'set_error', error: e.message }))
      .finally(() => dispatch({ type: 'set_busy', label: null }));
  }, [load]);
  const toggleRoute = useCallback((route: NotificationRoute) => {
    dispatch({ type: 'set_busy', label: `route:${route.id}` });
    void api.updateNotificationRoute(route.id, { enabled: !route.enabled, ownerKind: route.ownerKind })
      .then(load)
      .catch((e: Error) => dispatch({ type: 'set_error', error: e.message }))
      .finally(() => dispatch({ type: 'set_busy', label: null }));
  }, [load]);
  const testRoute = useCallback((route: NotificationRoute) => {
    dispatch({ type: 'set_busy', label: `test:${route.id}` });
    dispatch({ type: 'set_error', error: null });
    void api.testNotificationRoute(route.id, route.ownerKind)
      .then((request) => { dispatch({ type: 'set_notice', notice: `Created ${request.deliveryKind} request ${request.id}` }); load(); })
      .catch((e: Error) => dispatch({ type: 'set_error', error: e.message }))
      .finally(() => dispatch({ type: 'set_busy', label: null }));
  }, [load]);
  return (
    <section className="notification-settings-panel" data-testid="notification-settings-panel">
      <div className="notification-settings-head">
        <div className="sidebar-section-label">
          <Settings size={12} aria-hidden />
          Notification settings
        </div>
        <button type="button" className="icon-btn" title="Refresh notification settings" aria-label="Refresh notification settings" data-testid="notification-settings-refresh" disabled={state.busy !== null} onClick={load}>
          <RefreshCw size={12} className={state.busy === 'load' ? 'spin' : undefined} />
        </button>
      </div>
      {state.error && <PanelError>{state.error}</PanelError>}
      {state.notice && <div className="notification-settings-notice">{state.notice}</div>}
      <ChannelSection channels={state.channels} draft={state.channelDraft} busy={state.busy} configurableNotificationEmail={configurableNotificationEmail} onToggle={toggleChannel} onCreate={createChannel} onDraftChange={(update) => dispatch({ type: 'set_channel_draft', update })} onKindChange={(kind) => dispatch({ type: 'set_channel_kind', kind })} />
      <RouteSection routes={state.routes} draft={state.routeDraft} externalChannels={externalChannels} busy={state.busy} onToggle={toggleRoute} onTest={testRoute} onCreate={createRoute} onDraftChange={(update) => dispatch({ type: 'set_route_draft', update })} onModeChange={(mode) => dispatch({ type: 'set_route_mode', mode })} />
      <DeliverySection requests={state.requests} routes={state.routes} />
    </section>
  );
}

function ChannelIcon({ kind }: { kind: NotificationChannel['kind'] }) {
  if (kind === 'email') return <Mail size={13} />;
  if (kind === 'slack') return <Hash size={13} />;
  return <BellRing size={13} />;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="notification-settings-field">
      <span className="notification-settings-label">{label}</span>
      <div className="notification-settings-control">{children}</div>
    </div>
  );
}

function ChannelSection({
  channels, draft, busy, configurableNotificationEmail, onToggle, onCreate, onDraftChange, onKindChange,
}: {
  channels: NotificationChannel[];
  draft: ChannelDraft;
  busy: string | null;
  configurableNotificationEmail: boolean;
  onToggle: (channel: NotificationChannel) => void;
  onCreate: (draft: ChannelDraft) => void;
  onDraftChange: (update: Partial<ChannelDraft>) => void;
  onKindChange: (kind: ChannelDraftKind) => void;
}) {
  return (
    <section className="notification-settings-card">
      <div className="notification-settings-cardhead">
        <div className="notification-settings-kicker"><BellRing size={12} aria-hidden /> Channels</div>
        <p className="notification-settings-help">
          {configurableNotificationEmail
            ? 'Where notifications get delivered — in-app, email, or Slack.'
            : 'Where notifications get delivered — in-app or Slack.'}
        </p>
      </div>
      <div className="notification-settings-list">
        {channels.map((channel) => (
          <div className="notification-settings-row" data-testid="notification-channel-item" key={channel.id}>
            <div className="notification-settings-rowmain">
              <div className="notification-settings-title">
                <ChannelIcon kind={channel.kind} />
                {channelLabel(channel)}
              </div>
              <div className="notification-settings-meta">
                {channel.enabled ? 'enabled' : 'disabled'} · {channel.hasSecret ? 'secret saved' : 'no secret'}
              </div>
            </div>
            {channel.kind === 'in_app' ? (
              <span className="notification-settings-locked">built-in</span>
            ) : (
              <button type="button" className={`icon-btn ${channel.enabled ? 'is-on' : 'is-off'}`} title={channel.enabled ? 'Disable channel' : 'Enable channel'} aria-label={channel.enabled ? 'Disable channel' : 'Enable channel'} data-testid="notification-channel-toggle" disabled={busy === `channel:${channel.id}`} onClick={() => onToggle(channel)}>
                {channel.enabled ? <ToggleRight size={16} /> : <ToggleLeft size={16} />}
              </button>
            )}
          </div>
        ))}
      </div>
      <div className="notification-settings-form" data-testid="notification-channel-form">
        <div className="notification-settings-formkicker">New channel</div>
        <Field label="Kind">
          <PanelSelect className="row-height-select" data-testid="notification-channel-kind" value={draft.kind} onChange={(event) => onKindChange(event.target.value as ChannelDraftKind)}>
            <option value="slack">Slack</option>
            {configurableNotificationEmail && <option value="email">Email</option>}
          </PanelSelect>
        </Field>
        <Field label="Name">
          <input className="form-input" aria-label="Channel name" data-testid="notification-channel-name" value={draft.name} onChange={(event) => onDraftChange({ name: event.target.value })} placeholder="Channel name" />
        </Field>
        {draft.kind === 'email' ? (
          <>
            <Field label="To">
              <input className="form-input" aria-label="Email to address" data-testid="notification-channel-email-to" value={draft.to} onChange={(event) => onDraftChange({ to: event.target.value })} placeholder="alerts@example.com" />
            </Field>
            <Field label="From">
              <input className="form-input" aria-label="Email from address" data-testid="notification-channel-email-from" value={draft.from} onChange={(event) => onDraftChange({ from: event.target.value })} placeholder="frisket <alerts@example.com>" />
            </Field>
          </>
        ) : (
          <>
            <Field label="Channel">
              <input className="form-input" aria-label="Slack channel label" data-testid="notification-channel-slack-label" value={draft.channelLabel} onChange={(event) => onDraftChange({ channelLabel: event.target.value })} placeholder="#alerts" />
            </Field>
            <Field label="Webhook">
              <input className="form-input" aria-label="Slack webhook host" data-testid="notification-channel-slack-host" value={draft.webhookHost} onChange={(event) => onDraftChange({ webhookHost: event.target.value })} placeholder="hooks.slack.com" />
            </Field>
          </>
        )}
        <Field label="Secret ref">
          <input className="form-input" aria-label="API secret reference" data-testid="notification-channel-secret-ref" value={draft.secretRef} onChange={(event) => onDraftChange({ secretRef: event.target.value })} placeholder="env:SECRET_NAME" />
          <span className="notification-settings-hint">References a stored secret, e.g. <code>env:SECRET_NAME</code> — not the value itself.</span>
        </Field>
        <div className="notification-settings-formfooter">
          <label className="notification-settings-check">
            <input type="checkbox" checked={draft.enabled} onChange={(event) => onDraftChange({ enabled: event.target.checked })} />
            Enabled
          </label>
          <button type="button" className="btn btn-primary" data-testid="notification-create-channel" disabled={busy === 'channel'} onClick={() => onCreate(draft)}>
            <CheckCircle2 size={13} />
            Save channel
          </button>
        </div>
      </div>
    </section>
  );
}

function RouteSection({
  routes, draft, externalChannels, busy, onToggle, onTest, onCreate, onDraftChange, onModeChange,
}: {
  routes: NotificationRoute[];
  draft: RouteDraft;
  externalChannels: NotificationChannel[];
  busy: string | null;
  onToggle: (route: NotificationRoute) => void;
  onTest: (route: NotificationRoute) => void;
  onCreate: (draft: RouteDraft) => void;
  onDraftChange: (update: Partial<RouteDraft>) => void;
  onModeChange: (mode: RouteDraft['deliveryMode']) => void;
}) {
  return (
    <section className="notification-settings-card">
      <div className="notification-settings-cardhead">
        <div className="notification-settings-kicker"><Send size={12} aria-hidden /> Routes</div>
        <p className="notification-settings-help">Rules that match events to a channel and delivery cadence.</p>
      </div>
      <div className="notification-settings-list">
        {routes.map((route) => (
          <div className="notification-settings-row" data-testid="notification-route-item" key={route.id}>
            <div className="notification-settings-rowmain">
              <div className="notification-settings-title">{route.name}</div>
              <div className="notification-settings-meta">
                {route.enabled ? 'enabled' : 'disabled'} · {sourceMatchLabel(route)} · {routeModeLabel(route)}
              </div>
              {route.deliveryMode === 'digest' && (
                <div className="notification-digest-preview" data-testid="notification-digest-preview">
                  {routeModeLabel(route)}
                </div>
              )}
            </div>
            <div className="notification-settings-actions">
              <button type="button" className="secondary-btn notification-settings-test" title="Create route test request" aria-label="Create route test request" data-testid="notification-route-test" disabled={busy === `test:${route.id}`} onClick={() => onTest(route)}>
                <Send size={12} /> Test
              </button>
              <button type="button" className={`icon-btn ${route.enabled ? 'is-on' : 'is-off'}`} title={route.enabled ? 'Disable route' : 'Enable route'} aria-label={route.enabled ? 'Disable route' : 'Enable route'} data-testid="notification-route-toggle" disabled={busy === `route:${route.id}`} onClick={() => onToggle(route)}>
                {route.enabled ? <ToggleRight size={16} /> : <ToggleLeft size={16} />}
              </button>
            </div>
          </div>
        ))}
        {routes.length === 0 && (
          <PanelEmpty>No routes yet. Add a channel, then route events to it.</PanelEmpty>
        )}
      </div>
      <div className="notification-settings-form" data-testid="notification-route-form">
        <div className="notification-settings-formkicker">New route</div>
        <Field label="Name">
          <input className="form-input" aria-label="Route name" data-testid="notification-route-name" value={draft.name} onChange={(event) => onDraftChange({ name: event.target.value })} placeholder="Route name" />
        </Field>
        <Field label="Channel">
          <PanelSelect className="row-height-select" data-testid="notification-route-channel" value={draft.channelId} onChange={(event) => onDraftChange({ channelId: event.target.value })}>
            <option value="">Choose channel</option>
            {externalChannels.map((channel) => (
              <option key={channel.id} value={channel.id}>{channelLabel(channel)}</option>
            ))}
          </PanelSelect>
        </Field>
        <Field label="Source">
          <PanelSelect className="row-height-select" data-testid="notification-route-source-kind" value={draft.sourceKind} onChange={(event) => onDraftChange({ sourceKind: event.target.value as RouteDraft['sourceKind'] })}>
            <option value="watch">Watch</option>
            <option value="run">Run</option>
            <option value="source">Source</option>
            <option value="all">All</option>
          </PanelSelect>
        </Field>
        {draft.sourceKind === 'watch' && (
          <Field label="Watch ID">
            <input className="form-input" aria-label="Watch ID" data-testid="notification-route-watch-id" value={draft.watchId} onChange={(event) => onDraftChange({ watchId: event.target.value })} placeholder="Watch ID" />
          </Field>
        )}
        <Field label="Delivery">
          <PanelSelect className="row-height-select" data-testid="notification-route-mode" value={draft.deliveryMode} onChange={(event) => onModeChange(event.target.value as RouteDraft['deliveryMode'])}>
            <option value="immediate">Immediate</option>
            <option value="digest">Digest</option>
          </PanelSelect>
        </Field>
        {draft.deliveryMode === 'digest' && (
          <>
            <Field label="Cadence">
              <PanelSelect className="row-height-select" data-testid="notification-route-digest-cadence" value={draft.digestCadence} onChange={(event) => onDraftChange({ digestCadence: event.target.value as NotificationDigestCadence })}>
                <option value="daily">Daily</option>
                <option value="hourly">Hourly</option>
                <option value="manual">Manual</option>
              </PanelSelect>
            </Field>
            <Field label="At (UTC)">
              <input className="form-input" aria-label="Digest anchor time" data-testid="notification-route-digest-anchor" value={draft.digestAnchorTime} onChange={(event) => onDraftChange({ digestAnchorTime: event.target.value })} placeholder="09:00" />
            </Field>
          </>
        )}
        <Field label="Severity">
          <PanelSelect className="row-height-select" data-testid="notification-route-severity" value={draft.severityMin} onChange={(event) => onDraftChange({ severityMin: event.target.value as RouteDraft['severityMin'] })}>
            <option value="info">Info+</option>
            <option value="warning">Warning+</option>
            <option value="critical">Critical</option>
          </PanelSelect>
        </Field>
        <div className="notification-settings-formfooter">
          <label className="notification-settings-check">
            <input type="checkbox" checked={draft.enabled} onChange={(event) => onDraftChange({ enabled: event.target.checked })} />
            Enabled
          </label>
          <button type="button" className="btn btn-primary" data-testid="notification-create-route" disabled={!draft.channelId || busy === 'route'} onClick={() => onCreate(draft)}>
            <CheckCircle2 size={13} />
            Save route
          </button>
        </div>
      </div>
    </section>
  );
}

function DeliverySection({ requests, routes }: { requests: NotificationDeliveryRequest[]; routes: NotificationRoute[] }) {
  return (
    <section className="notification-settings-card">
      <div className="notification-settings-cardhead">
        <div className="notification-settings-kicker"><Activity size={12} aria-hidden /> Delivery health</div>
        <p className="notification-settings-help">Recent delivery attempts across all routes.</p>
      </div>
      <div className="notification-settings-list">
        {requests.map((request) => (
          <div className="notification-settings-row" data-testid="notification-delivery-request" key={request.id}>
            <div className="notification-settings-rowmain">
              <div className="notification-settings-title">
                {request.deliveryKind} request {request.id}
              </div>
              <div className="notification-settings-meta">
                {requestRouteLabel(request, routes)} · channel {request.channelId}
              </div>
              {request.lastError && (
                <div className="notification-settings-error-line">{request.lastError}</div>
              )}
            </div>
            <StatusChip tone={statusChipTone(request.status)} size="sm" uppercase>
              {request.status}
            </StatusChip>
          </div>
        ))}
        {requests.length === 0 && <PanelEmpty>No delivery requests yet.</PanelEmpty>}
      </div>
    </section>
  );
}
