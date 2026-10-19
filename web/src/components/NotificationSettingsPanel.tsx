import { Button } from './Button';
import { FormField, TextInput } from './FormControls';
import { IconButton } from './IconButton';
import { useCallback, useEffect, useMemo, useReducer } from 'react';
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
import './NotificationSettingsPanel.css';

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
    <section className="panel-card" data-testid="notification-settings-panel">
      <div className="control-row">
        <div className="control-row panel-item-title panel-item-main">
          <Settings size={12} aria-hidden />
          Notification settings
        </div>
        <button type="button" className="icon-btn" title="Refresh notification settings" aria-label="Refresh notification settings" data-testid="notification-settings-refresh" disabled={state.busy !== null} onClick={load}>
          <RefreshCw size={12} className={state.busy === 'load' ? 'spin' : undefined} />
        </button>
      </div>
      {state.error && <PanelError>{state.error}</PanelError>}
      {state.notice && <div className="panel-notice" data-tone="warning">{state.notice}</div>}
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
    <section className="panel-card">
      <div>
        <div className="control-row panel-item-title"><BellRing size={12} aria-hidden /> Channels</div>
        <p className="panel-item-description">
          {configurableNotificationEmail
            ? 'Where notifications get delivered — in-app, email, or Slack.'
            : 'Where notifications get delivered — in-app or Slack.'}
        </p>
      </div>
      <div className="panel-list">
        {channels.map((channel) => (
          <div className="panel-card" data-layout="row" data-testid="notification-channel-item" key={channel.id}>
            <div className="panel-item-main">
              <div className="control-row panel-item-title">
                <ChannelIcon kind={channel.kind} />
                {channelLabel(channel)}
              </div>
              <div className="panel-item-meta">
                {channel.enabled ? 'enabled' : 'disabled'} · {channel.hasSecret ? 'secret saved' : 'no secret'}
              </div>
            </div>
            {channel.kind === 'in_app' ? (
              <StatusChip tone="neutral" size="sm">built-in</StatusChip>
            ) : (
              <IconButton icon={channel.enabled ? ToggleRight : ToggleLeft} aria-pressed={channel.enabled} title={channel.enabled ? 'Disable channel' : 'Enable channel'} label={channel.enabled ? 'Disable channel' : 'Enable channel'} data-testid="notification-channel-toggle" disabled={busy === `channel:${channel.id}`} onClick={() => onToggle(channel)} />
            )}
          </div>
        ))}
      </div>
      <div className="panel-card" data-testid="notification-channel-form">
        <div className="panel-item-title">New channel</div>
        <FormField layout="inline" label="Kind">
          <PanelSelect data-testid="notification-channel-kind" value={draft.kind} onChange={(event) => onKindChange(event.target.value as ChannelDraftKind)}>
            <option value="slack">Slack</option>
            {configurableNotificationEmail && <option value="email">Email</option>}
          </PanelSelect>
        </FormField>
        <FormField layout="inline" label="Name">
          <TextInput aria-label="Channel name" data-testid="notification-channel-name" value={draft.name} onChange={(event) => onDraftChange({ name: event.target.value })} placeholder="Channel name" />
        </FormField>
        {draft.kind === 'email' ? (
          <>
            <FormField layout="inline" label="To">
              <TextInput aria-label="Email to address" data-testid="notification-channel-email-to" value={draft.to} onChange={(event) => onDraftChange({ to: event.target.value })} placeholder="alerts@example.com" />
            </FormField>
            <FormField layout="inline" label="From">
              <TextInput aria-label="Email from address" data-testid="notification-channel-email-from" value={draft.from} onChange={(event) => onDraftChange({ from: event.target.value })} placeholder="frisket <alerts@example.com>" />
            </FormField>
          </>
        ) : (
          <>
            <FormField layout="inline" label="Channel">
              <TextInput aria-label="Slack channel label" data-testid="notification-channel-slack-label" value={draft.channelLabel} onChange={(event) => onDraftChange({ channelLabel: event.target.value })} placeholder="#alerts" />
            </FormField>
            <FormField layout="inline" label="Webhook">
              <TextInput aria-label="Slack webhook host" data-testid="notification-channel-slack-host" value={draft.webhookHost} onChange={(event) => onDraftChange({ webhookHost: event.target.value })} placeholder="hooks.slack.com" />
            </FormField>
          </>
        )}
        <FormField layout="inline" label="Secret ref">
          <TextInput aria-label="API secret reference" data-testid="notification-channel-secret-ref" value={draft.secretRef} onChange={(event) => onDraftChange({ secretRef: event.target.value })} placeholder="env:SECRET_NAME" />
          <span className="form-hint">References a stored secret, e.g. <code>env:SECRET_NAME</code> — not the value itself.</span>
        </FormField>
        <div className="control-row">
          <label className="control-row panel-item-description panel-item-main">
            <input type="checkbox" checked={draft.enabled} onChange={(event) => onDraftChange({ enabled: event.target.checked })} />
            Enabled
          </label>
          <Button type="button" className="btn-primary" data-testid="notification-create-channel" disabled={busy === 'channel'} onClick={() => onCreate(draft)}>
            <CheckCircle2 size={13} />
            Save channel
          </Button>
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
    <section className="panel-card">
      <div>
        <div className="control-row panel-item-title"><Send size={12} aria-hidden /> Routes</div>
        <p className="panel-item-description">Rules that match events to a channel and delivery cadence.</p>
      </div>
      <div className="panel-list">
        {routes.map((route) => (
          <div className="panel-card" data-layout="row" data-testid="notification-route-item" key={route.id}>
            <div className="panel-item-main">
              <div className="panel-item-title">{route.name}</div>
              <div className="panel-item-meta">
                {route.enabled ? 'enabled' : 'disabled'} · {sourceMatchLabel(route)} · {routeModeLabel(route)}
              </div>
              {route.deliveryMode === 'digest' && (
                <div className="panel-item-meta notification-digest-preview" data-testid="notification-digest-preview">
                  {routeModeLabel(route)}
                </div>
              )}
            </div>
            <div className="control-row">
              <Button type="button" title="Create route test request" aria-label="Create route test request" data-testid="notification-route-test" disabled={busy === `test:${route.id}`} onClick={() => onTest(route)}>
                <Send size={12} /> Test
              </Button>
              <IconButton icon={route.enabled ? ToggleRight : ToggleLeft} aria-pressed={route.enabled} title={route.enabled ? 'Disable route' : 'Enable route'} label={route.enabled ? 'Disable route' : 'Enable route'} data-testid="notification-route-toggle" disabled={busy === `route:${route.id}`} onClick={() => onToggle(route)} />
            </div>
          </div>
        ))}
        {routes.length === 0 && (
          <PanelEmpty>No routes yet. Add a channel, then route events to it.</PanelEmpty>
        )}
      </div>
      <div className="panel-card" data-testid="notification-route-form">
        <div className="panel-item-title">New route</div>
        <FormField layout="inline" label="Name">
          <TextInput aria-label="Route name" data-testid="notification-route-name" value={draft.name} onChange={(event) => onDraftChange({ name: event.target.value })} placeholder="Route name" />
        </FormField>
        <FormField layout="inline" label="Channel">
          <PanelSelect data-testid="notification-route-channel" value={draft.channelId} onChange={(event) => onDraftChange({ channelId: event.target.value })}>
            <option value="">Choose channel</option>
            {externalChannels.map((channel) => (
              <option key={channel.id} value={channel.id}>{channelLabel(channel)}</option>
            ))}
          </PanelSelect>
        </FormField>
        <FormField layout="inline" label="Source">
          <PanelSelect data-testid="notification-route-source-kind" value={draft.sourceKind} onChange={(event) => onDraftChange({ sourceKind: event.target.value as RouteDraft['sourceKind'] })}>
            <option value="watch">Watch</option>
            <option value="run">Run</option>
            <option value="source">Source</option>
            <option value="all">All</option>
          </PanelSelect>
        </FormField>
        {draft.sourceKind === 'watch' && (
          <FormField layout="inline" label="Watch ID">
            <TextInput aria-label="Watch ID" data-testid="notification-route-watch-id" value={draft.watchId} onChange={(event) => onDraftChange({ watchId: event.target.value })} placeholder="Watch ID" />
          </FormField>
        )}
        <FormField layout="inline" label="Delivery">
          <PanelSelect data-testid="notification-route-mode" value={draft.deliveryMode} onChange={(event) => onModeChange(event.target.value as RouteDraft['deliveryMode'])}>
            <option value="immediate">Immediate</option>
            <option value="digest">Digest</option>
          </PanelSelect>
        </FormField>
        {draft.deliveryMode === 'digest' && (
          <>
            <FormField layout="inline" label="Cadence">
              <PanelSelect data-testid="notification-route-digest-cadence" value={draft.digestCadence} onChange={(event) => onDraftChange({ digestCadence: event.target.value as NotificationDigestCadence })}>
                <option value="daily">Daily</option>
                <option value="hourly">Hourly</option>
                <option value="manual">Manual</option>
              </PanelSelect>
            </FormField>
            <FormField layout="inline" label="At (UTC)">
              <TextInput aria-label="Digest anchor time" data-testid="notification-route-digest-anchor" value={draft.digestAnchorTime} onChange={(event) => onDraftChange({ digestAnchorTime: event.target.value })} placeholder="09:00" />
            </FormField>
          </>
        )}
        <FormField layout="inline" label="Severity">
          <PanelSelect data-testid="notification-route-severity" value={draft.severityMin} onChange={(event) => onDraftChange({ severityMin: event.target.value as RouteDraft['severityMin'] })}>
            <option value="info">Info+</option>
            <option value="warning">Warning+</option>
            <option value="critical">Critical</option>
          </PanelSelect>
        </FormField>
        <div className="control-row">
          <label className="control-row panel-item-description panel-item-main">
            <input type="checkbox" checked={draft.enabled} onChange={(event) => onDraftChange({ enabled: event.target.checked })} />
            Enabled
          </label>
          <Button type="button" className="btn-primary" data-testid="notification-create-route" disabled={!draft.channelId || busy === 'route'} onClick={() => onCreate(draft)}>
            <CheckCircle2 size={13} />
            Save route
          </Button>
        </div>
      </div>
    </section>
  );
}

function DeliverySection({ requests, routes }: { requests: NotificationDeliveryRequest[]; routes: NotificationRoute[] }) {
  return (
    <section className="panel-card">
      <div>
        <div className="control-row panel-item-title"><Activity size={12} aria-hidden /> Delivery health</div>
        <p className="panel-item-description">Recent delivery attempts across all routes.</p>
      </div>
      <div className="panel-list">
        {requests.map((request) => (
          <div className="panel-card" data-layout="row" data-testid="notification-delivery-request" key={request.id}>
            <div className="panel-item-main">
              <div className="panel-item-title">
                {request.deliveryKind} request {request.id}
              </div>
              <div className="panel-item-meta">
                {requestRouteLabel(request, routes)} · channel {request.channelId}
              </div>
              {request.lastError && (
                <div className="panel-item-meta notification-settings-error-line">{request.lastError}</div>
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
