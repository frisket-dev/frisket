import type { HttpContractOperationMap } from '../generated/openHttpContracts';
import { httpContract } from './httpContract';

type SetModelRouteRequest = HttpContractOperationMap['tenant.set_project_model_route.patch']['request'];
type SetModelRouteResponse = HttpContractOperationMap['tenant.set_project_model_route.patch']['responses']['200'];
export type ProjectModelRoute = SetModelRouteRequest['route'];

export interface ProjectModelRoutesApi {
  setProjectModelRoute(
    projectId: string,
    modelId: string,
    route: ProjectModelRoute,
    signal?: AbortSignal,
  ): Promise<SetModelRouteResponse>;
}

export const projectModelRoutesApi: ProjectModelRoutesApi = {
  setProjectModelRoute(projectId, modelId, route, signal) {
    const body: SetModelRouteRequest = { model_id: modelId, route };
    return httpContract('tenant.set_project_model_route.patch', {
      pathParams: { pid: projectId }, query: {}, body, signal,
    });
  },
};
