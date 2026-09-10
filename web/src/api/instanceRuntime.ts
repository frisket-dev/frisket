import type { HttpContractSuccessResponse } from "./httpContract";
import { httpContract } from "./httpContract";
import type { HealthStatus, InstanceIdentity, RuntimeConfig } from "./types";

export interface InstanceRuntimeOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface InstanceRuntimeApi {
  getRuntimeConfig(options?: InstanceRuntimeOptions): Promise<RuntimeConfig>;
  updateRuntimeConfig(
    cacheMode: RuntimeConfig['cache_mode'] | undefined,
    confirmed: boolean,
    costPreapprovalUsd?: string,
    options?: InstanceRuntimeOptions,
  ): Promise<RuntimeConfig>;
  getHealth(options?: InstanceRuntimeOptions): Promise<HealthStatus>;
  getInstanceInfo(options?: InstanceRuntimeOptions): Promise<InstanceIdentity>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type RuntimeConfigWire = HttpContractSuccessResponse<"tenant.runtime_config.get">;
type RuntimeConfigUpdateWire = HttpContractSuccessResponse<"tenant.update_runtime_config.patch">;
type HealthWire = HttpContractSuccessResponse<"tenant.health.get">;

function mapRuntimeConfig(wire: RuntimeConfigWire): RuntimeConfig {
  // These application fields remain broader on the server contract than in
  // the browser domain model, so normalize them at the transport boundary.
  return {
    ...wire,
    in_container: wire.in_container as RuntimeConfig["in_container"],
    recipe_fence_posture: wire.recipe_fence_posture as RuntimeConfig["recipe_fence_posture"],
  };
}

function mapHealth(wire: HealthWire): HealthStatus {
  return {
    ...wire,
    posture: wire.posture as HealthStatus["posture"],
    tier: wire.tier as HealthStatus["tier"],
  };
}

export function createInstanceRuntimeApi(
  errorFactory?: ContractErrorFactory,
): InstanceRuntimeApi {
  return {
    getRuntimeConfig(options = {}) {
      return httpContract(
        "tenant.runtime_config.get",
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapRuntimeConfig,
      );
    },

    updateRuntimeConfig(cacheMode, confirmed, costPreapprovalUsd, options = {}) {
      return httpContract(
        "tenant.update_runtime_config.patch",
        {
          pathParams: {},
          query: {},
          body: {
            cache_mode: cacheMode,
            cost_preapproval_usd: costPreapprovalUsd,
            confirmed,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        (wire: RuntimeConfigUpdateWire) => mapRuntimeConfig(wire),
      );
    },

    getHealth(options = {}) {
      return httpContract(
        "tenant.health.get",
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        mapHealth,
      );
    },

    getInstanceInfo(options = {}) {
      return httpContract(
        "outer.instance_info.get",
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
