import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from "./httpContract";
import type {
  OrgKeyInfo,
  ProviderCatalog,
  ProviderValidateResult,
} from "./types";

type OrgKeySaveWire = HttpContractSuccessResponse<"outer.set_org_key.post">;
type OrgKeyDeleteWire = HttpContractSuccessResponse<"outer.delete_org_key.delete">;

function discardSavedKey(wire: OrgKeySaveWire): void {
  void wire;
}

function discardDeletedKey(wire: OrgKeyDeleteWire): void {
  void wire;
}

export function createOrganizationProvidersApi() {
  return {
    providerCatalog(options: RequestOptions = {}): Promise<ProviderCatalog> {
      return httpContract(
        "tenant.provider_catalog.get",
        {
          signal: options.signal,
        },
      );
    },

    listOrgKeys(options: RequestOptions = {}): Promise<OrgKeyInfo[]> {
      return httpContract(
        "outer.list_org_keys.get",
        {
          signal: options.signal,
        },
      );
    },

    setOrgKey(
      provider: string,
      key: string,
      validationToken?: string | null,
      options: RequestOptions = {},
    ): Promise<void> {
      return httpContract(
        "outer.set_org_key.post",
        {
          body: { provider, key, validation_token: validationToken ?? null },
          signal: options.signal,
        },
        discardSavedKey,
      );
    },

    validateOrgKey(
      provider: string,
      key?: string,
      options: RequestOptions = {},
    ): Promise<ProviderValidateResult> {
      return httpContract(
        "outer.validate_org_key.post",
        {
          body: key ? { provider, key } : { provider },
          signal: options.signal,
        },
      );
    },

    deleteOrgKey(provider: string, options: RequestOptions = {}): Promise<void> {
      return httpContract(
        "outer.delete_org_key.delete",
        {
          pathParams: { provider },
          signal: options.signal,
        },
        discardDeletedKey,
      );
    },
  };
}
