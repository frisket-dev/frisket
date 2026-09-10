import { httpContract } from "./httpContract";
import type { ApiTokenCreateResponse, ApiTokenInfo } from "./types";

export interface ApiTokensOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface ApiTokenRevokeResponse {
  ok: boolean;
  revoked: boolean;
}

export interface ApiTokensApi {
  listApiKeys(options?: ApiTokensOptions): Promise<ApiTokenInfo[]>;
  createApiKey(
    name: string,
    options?: ApiTokensOptions,
  ): Promise<ApiTokenCreateResponse>;
  revokeApiKey(
    tokenId: number | string,
    options?: ApiTokensOptions,
  ): Promise<ApiTokenRevokeResponse>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
export function createApiTokensApi(errorFactory: ContractErrorFactory): ApiTokensApi {
  return {
    listApiKeys(options = {}) {
      return httpContract(
        "outer.list_tokens.get",
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    createApiKey(name, options = {}) {
      return httpContract(
        "outer.create_token.post",
        {
          pathParams: {},
          query: {},
          body: { name },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    revokeApiKey(tokenId, options = {}) {
      return httpContract(
        "outer.revoke_token.delete",
        {
          // Preserve the previous number|string public API and let the route
          // retain responsibility for deciding whether a path value is valid.
          pathParams: { token_id: tokenId as never },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
