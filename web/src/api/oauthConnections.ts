import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from './httpContract';
import type { OAuthConnectionInfo } from './types';

interface OAuthConnectionsApi {
  listOAuthConnections(
    provider?: string,
    options?: RequestOptions,
  ): Promise<OAuthConnectionInfo[]>;
}

type OAuthConnectionListWire =
  HttpContractSuccessResponse<'outer.list_oauth_connections.get'>;

function mapOAuthConnectionList(wire: OAuthConnectionListWire): OAuthConnectionInfo[] {
  return wire.connections.map(({ connection_id, scopes, ...connection }) => ({
    ...connection,
    ...(connection_id == null ? {} : { connection_id }),
    ...(scopes == null ? {} : { scopes }),
  }));
}

export function createOAuthConnectionsApi(
): OAuthConnectionsApi {
  return {
    listOAuthConnections(provider, options = {}) {
      return httpContract(
        'outer.list_oauth_connections.get',
        {
          query: { provider },
          signal: options.signal,
        },
        mapOAuthConnectionList,
      );
    },
  };
}
