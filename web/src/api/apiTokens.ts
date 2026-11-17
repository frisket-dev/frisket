import { httpContract, type RequestOptions } from "./httpContract";
import type { ApiTokenCreateResponse, ApiTokenInfo } from "./types";

interface ApiTokenRevokeResponse {
  ok: boolean;
  revoked: boolean;
}

interface ApiTokensApi {
  listApiKeys(options?: RequestOptions): Promise<ApiTokenInfo[]>;
  createApiKey(
    name: string,
    options?: RequestOptions,
  ): Promise<ApiTokenCreateResponse>;
  revokeApiKey(
    tokenId: number | string,
    options?: RequestOptions,
  ): Promise<ApiTokenRevokeResponse>;
}

export function createApiTokensApi(): ApiTokensApi {
  return {
    listApiKeys(options = {}) {
      return httpContract(
        "outer.list_tokens.get",
        {
          signal: options.signal,
        },
      );
    },

    createApiKey(name, options = {}) {
      return httpContract(
        "outer.create_token.post",
        {
          body: { name },
          signal: options.signal,
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
          signal: options.signal,
        },
      );
    },
  };
}
