import { httpContract, type HttpContractSuccessResponse } from "./httpContract";
import type {
  OrgKeyInfo,
  ProviderCatalog,
  ProviderValidateResult,
} from "./types";

export interface OrganizationProvidersOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface OrganizationProvidersApi {
  providerCatalog(options?: OrganizationProvidersOptions): Promise<ProviderCatalog>;
  listOrgKeys(options?: OrganizationProvidersOptions): Promise<OrgKeyInfo[]>;
  setOrgKey(
    provider: string,
    key: string,
    validationToken?: string | null,
    options?: OrganizationProvidersOptions,
  ): Promise<void>;
  validateOrgKey(
    provider: string,
    key?: string,
    options?: OrganizationProvidersOptions,
  ): Promise<ProviderValidateResult>;
  deleteOrgKey(
    provider: string,
    options?: OrganizationProvidersOptions,
  ): Promise<void>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type OrgKeySaveWire = HttpContractSuccessResponse<"outer.set_org_key.post">;
type OrgKeyDeleteWire = HttpContractSuccessResponse<"outer.delete_org_key.delete">;

function discardSavedKey(wire: OrgKeySaveWire): void {
  void wire;
}

function discardDeletedKey(wire: OrgKeyDeleteWire): void {
  void wire;
}

export function createOrganizationProvidersApi(
  errorFactory: ContractErrorFactory,
): OrganizationProvidersApi {
  return {
    providerCatalog(options = {}) {
      return httpContract(
        "tenant.provider_catalog.get",
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    listOrgKeys(options = {}) {
      return httpContract(
        "outer.list_org_keys.get",
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    setOrgKey(provider, key, validationToken, options = {}) {
      return httpContract(
        "outer.set_org_key.post",
        {
          pathParams: {},
          query: {},
          body: { provider, key, validation_token: validationToken ?? null },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        discardSavedKey,
      );
    },

    validateOrgKey(provider, key, options = {}) {
      return httpContract(
        "outer.validate_org_key.post",
        {
          pathParams: {},
          query: {},
          body: key ? { provider, key } : { provider },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    deleteOrgKey(provider, options = {}) {
      return httpContract(
        "outer.delete_org_key.delete",
        {
          pathParams: { provider },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        discardDeletedKey,
      );
    },
  };
}
