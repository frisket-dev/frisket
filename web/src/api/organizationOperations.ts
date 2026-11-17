import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from './httpContract';
import type { MediaProxyStatus, OrgEnvInfo } from './types';

type OrganizationEnvSaveWire =
  HttpContractSuccessResponse<'outer.set_org_env_var.post'>;
type OrganizationEnvDeleteWire =
  HttpContractSuccessResponse<'outer.delete_org_env_var.delete'>;
type OrganizationMediaProxyStatusWire =
  HttpContractSuccessResponse<'outer.org_media_proxy_status.get'>;

function discardEnvSave(wire: OrganizationEnvSaveWire): void {
  void wire;
}

function discardEnvDelete(wire: OrganizationEnvDeleteWire): void {
  void wire;
}

function mapMediaStatus(wire: OrganizationMediaProxyStatusWire): MediaProxyStatus {
  return {
    configured: wire.configured,
    connected: wire.connected,
    canConfigure: wire.can_configure,
  };
}

export function createOrganizationOperationsApi() {
  return {
    listOrgEnvVars(options: RequestOptions = {}): Promise<OrgEnvInfo[]> {
      return httpContract(
        'outer.list_org_env_vars.get',
        {
          signal: options.signal,
        },
      );
    },

    setOrgEnvVar(name: string, value: string, options: RequestOptions = {}): Promise<void> {
      return httpContract(
        'outer.set_org_env_var.post',
        {
          body: { name, value },
          signal: options.signal,
        },
        discardEnvSave,
      );
    },

    deleteOrgEnvVar(name: string, options: RequestOptions = {}): Promise<void> {
      return httpContract(
        'outer.delete_org_env_var.delete',
        {
          pathParams: { name },
          signal: options.signal,
        },
        discardEnvDelete,
      );
    },

    /** Egress-proxy status for the youtube_provider_blocked remediation card.
     *  Local tier (no org identity) 404s — callers branch on ApiError.status. */
    getMediaProxyStatus(options: RequestOptions = {}): Promise<MediaProxyStatus> {
      return httpContract(
        'outer.org_media_proxy_status.get',
        {
          signal: options.signal,
        },
        mapMediaStatus,
      );
    },
  };
}
