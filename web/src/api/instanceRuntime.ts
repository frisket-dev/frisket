import type { HttpContractSuccessResponse } from "./httpContract";
import { httpContract, type RequestOptions } from "./httpContract";
import type { HealthStatus, InstanceIdentity, RuntimeConfig } from "./types";

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

export function createInstanceRuntimeApi() {
  return {
    getRuntimeConfig(options: RequestOptions = {}): Promise<RuntimeConfig> {
      return httpContract(
        "tenant.runtime_config.get",
        {
          signal: options.signal,
        },
        mapRuntimeConfig,
      );
    },

    updateRuntimeConfig(
      cacheMode: RuntimeConfig['cache_mode'] | undefined,
      confirmed: boolean,
      costPreapprovalUsd?: string,
      options: RequestOptions = {},
    ): Promise<RuntimeConfig> {
      return httpContract(
        "tenant.update_runtime_config.patch",
        {
          body: {
            cache_mode: cacheMode,
            cost_preapproval_usd: costPreapprovalUsd,
            confirmed,
          },
          signal: options.signal,
        },
        (wire: RuntimeConfigUpdateWire) => mapRuntimeConfig(wire),
      );
    },

    getHealth(options: RequestOptions = {}): Promise<HealthStatus> {
      return httpContract(
        "tenant.health.get",
        {
          signal: options.signal,
        },
        mapHealth,
      );
    },

    getInstanceInfo(options: RequestOptions = {}): Promise<InstanceIdentity> {
      return httpContract(
        "outer.instance_info.get",
        {
          signal: options.signal,
        },
      );
    },
  };
}
