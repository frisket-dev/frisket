import type { HttpContractOperationMap } from '../generated/openHttpContracts';
import { httpContract } from './httpContract';

type SetModelRouteRequest = HttpContractOperationMap['tenant.set_project_model_route.patch']['request'];
type SetModelRouteResponse = HttpContractOperationMap['tenant.set_project_model_route.patch']['responses']['200'];
type ProjectModelRoute = SetModelRouteRequest['route'];

export const projectModelRoutesApi = {
  setProjectModelRoute(
    projectId: string,
    modelId: string,
    route: ProjectModelRoute,
    signal?: AbortSignal,
  ): Promise<SetModelRouteResponse> {
    const body: SetModelRouteRequest = { model_id: modelId, route };
    return httpContract('tenant.set_project_model_route.patch', {
      pathParams: { pid: projectId }, body, signal,
    });
  },
};
