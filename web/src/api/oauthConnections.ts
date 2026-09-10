import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type { OAuthConnectionInfo } from './types';

export interface OAuthConnectionsOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface OAuthConnectionsApi {
  listOAuthConnections(
    provider?: string,
    options?: OAuthConnectionsOptions,
  ): Promise<OAuthConnectionInfo[]>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
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
  errorFactory: ContractErrorFactory,
): OAuthConnectionsApi {
  return {
    listOAuthConnections(provider, options = {}) {
      return httpContract(
        'outer.list_oauth_connections.get',
        {
          pathParams: {},
          query: { provider },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapOAuthConnectionList,
      );
    },
  };
}
