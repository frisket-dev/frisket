import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from './httpContract';
import type { OAuthConnectionInfo } from './types';

type OAuthConnectionListWire =
  HttpContractSuccessResponse<'outer.list_oauth_connections.get'>;

function mapOAuthConnectionList(wire: OAuthConnectionListWire): OAuthConnectionInfo[] {
  return wire.connections.map(({ connection_id, scopes, ...connection }) => ({
    ...connection,
    ...(connection_id == null ? {} : { connection_id }),
    ...(scopes == null ? {} : { scopes }),
  }));
}

export function createOAuthConnectionsApi() {
  return {
    listOAuthConnections(
      provider?: string,
      options: RequestOptions = {},
    ): Promise<OAuthConnectionInfo[]> {
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
