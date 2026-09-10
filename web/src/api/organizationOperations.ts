import { httpContract, type HttpContractSuccessResponse } from './httpContract';
import type { MediaProxyStatus, OrgEnvInfo } from './types';

export interface OrganizationOperationsOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface OrganizationOperationsApi {
  listOrgEnvVars(options?: OrganizationOperationsOptions): Promise<OrgEnvInfo[]>;
  setOrgEnvVar(
    name: string,
    value: string,
    options?: OrganizationOperationsOptions,
  ): Promise<void>;
  deleteOrgEnvVar(
    name: string,
    options?: OrganizationOperationsOptions,
  ): Promise<void>;
  getMediaProxyStatus(options?: OrganizationOperationsOptions): Promise<MediaProxyStatus>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
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

export function createOrganizationOperationsApi(
  errorFactory: ContractErrorFactory,
): OrganizationOperationsApi {
  return {
    listOrgEnvVars(options = {}) {
      return httpContract(
        'outer.list_org_env_vars.get',
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    setOrgEnvVar(name, value, options = {}) {
      return httpContract(
        'outer.set_org_env_var.post',
        {
          pathParams: {},
          query: {},
          body: { name, value },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        discardEnvSave,
      );
    },

    deleteOrgEnvVar(name, options = {}) {
      return httpContract(
        'outer.delete_org_env_var.delete',
        {
          pathParams: { name },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        discardEnvDelete,
      );
    },

    getMediaProxyStatus(options = {}) {
      return httpContract(
        'outer.org_media_proxy_status.get',
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapMediaStatus,
      );
    },
  };
}
