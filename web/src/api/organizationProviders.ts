import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from "./httpContract";
import type {
  OrganizationKeyInfo,
  ProviderCatalog,
  ProviderValidateResult,
} from "./types";

type OrganizationKeySaveWire = HttpContractSuccessResponse<"outer.set_org_key.post">;
type OrganizationKeyDeleteWire = HttpContractSuccessResponse<"outer.delete_org_key.delete">;

function discardSavedKey(wire: OrganizationKeySaveWire): void {
  void wire;
}

function discardDeletedKey(wire: OrganizationKeyDeleteWire): void {
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

    listOrganizationKeys(options: RequestOptions = {}): Promise<OrganizationKeyInfo[]> {
      return httpContract(
        "outer.list_org_keys.get",
        {
          signal: options.signal,
        },
      );
    },

    setOrganizationKey(
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

    validateOrganizationKey(
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

    deleteOrganizationKey(provider: string, options: RequestOptions = {}): Promise<void> {
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
