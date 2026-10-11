import type {
  HttpCloudConnectionResponse,
  HttpProjectCloudRoutingPatch,
  HttpProjectCloudRoutingResponse,
} from '../generated/openHttpContracts';
import type {
  CloudAccountView,
  CloudConnectionClient,
  CloudConnectionView,
} from '../cloud/CloudConnectionPanel';
import type { CloudConnectionPreferencesClient } from '../cloud/CloudProfileSettings';
import type {
  ProjectCloudRoutingClient,
  ProjectCloudRoutingView,
} from '../cloud/ProjectCloudRoutingSettings';
import { httpContract } from './httpContract';

type ConnectionWire = HttpCloudConnectionResponse;

const accountView = (account: ConnectionWire['accounts'][number]): CloudAccountView => ({
  id: account.id,
  name: account.name,
  kind: account.kind,
  eligible: account.eligible,
  eligibilityReason: account.eligibility_reason ?? undefined,
  balanceLabel: account.balance_label ?? undefined,
  balanceUrl: account.balance_url ?? undefined,
});

const connectionView = (wire: ConnectionWire): CloudConnectionView => ({
  state: wire.state,
  identity: wire.identity ? {
    id: wire.identity.id,
    displayName: wire.identity.display_name,
    email: wire.identity.email,
  } : null,
  expiresAt: wire.expires_at ?? undefined,
  accounts: wire.accounts.map(accountView),
  capabilities: {
    canConnect: wire.capabilities.can_connect,
    canReconnect: wire.capabilities.can_reconnect,
    canDisconnect: wire.capabilities.can_disconnect,
  },
});

export const cloudConnectionApi: CloudConnectionClient = {
  getConnection(signal) {
    return httpContract('tenant.get_cloud_connection.get', {
      pathParams: {}, query: {}, signal,
    }, connectionView);
  },
  startAuthorization(signal) {
    return httpContract('tenant.begin_cloud_authorization.post', {
      pathParams: {}, query: {}, signal,
    }, (wire) => ({
      id: wire.id,
      userCode: wire.user_code,
      verificationUri: wire.verification_uri,
      verificationUriComplete: wire.verification_uri_complete ?? undefined,
      expiresAt: wire.expires_at,
      pollIntervalSeconds: wire.poll_interval_seconds,
    }));
  },
  pollAuthorization(id, signal) {
    return httpContract('tenant.poll_cloud_authorization.get', {
      pathParams: { authorization_id: id }, query: {}, signal,
    }, (wire) => ({ status: wire.status, connection: connectionView(wire.connection) }));
  },
  cancelAuthorization(id, signal) {
    return httpContract('tenant.cancel_cloud_authorization.delete', {
      pathParams: { authorization_id: id }, query: {}, signal,
    }).then(() => undefined);
  },
  disconnect(signal) {
    return httpContract('tenant.delete_cloud_connection.delete', {
      pathParams: {}, query: {}, signal,
    }, (wire) => ({
      ...connectionView(wire.connection),
      notice: wire.warning ?? undefined,
    }));
  },
};

export const cloudConnectionPreferencesApi: CloudConnectionPreferencesClient = {
  get(signal) {
    return httpContract('tenant.get_cloud_preferences.get', {
      pathParams: {}, query: {}, signal,
    }, (wire) => wire.preference);
  },
  patch(preference, signal) {
    return httpContract('tenant.patch_cloud_preferences.patch', {
      pathParams: {}, query: {}, body: { preference }, signal,
    }, (wire) => wire.preference);
  },
};

const projectRoutingView = (wire: HttpProjectCloudRoutingResponse): ProjectCloudRoutingView => ({
  connectionState: wire.connection_state,
  selectedAccountId: wire.selected_account_id,
  preference: wire.preference,
  preferenceInherited: wire.preference_inherited,
  accounts: wire.accounts.map((account) => accountView(account)),
  overlapProviderLabels: wire.overlap_provider_labels,
  canMutate: wire.can_mutate,
});

export const projectCloudRoutingApi: ProjectCloudRoutingClient = {
  get(projectId, signal) {
    return httpContract('tenant.get_project_cloud_routing.get', {
      pathParams: { pid: projectId }, query: {}, signal,
    }, projectRoutingView);
  },
  patch(projectId, patch, signal) {
    const body: HttpProjectCloudRoutingPatch = {
      ...(patch.selectedAccountId === undefined ? {} : { selected_account_id: patch.selectedAccountId }),
      ...(patch.preference === undefined ? {} : { preference: patch.preference }),
    };
    return httpContract('tenant.patch_project_cloud_routing.patch', {
      pathParams: { pid: projectId }, query: {}, body, signal,
    }, projectRoutingView);
  },
};
